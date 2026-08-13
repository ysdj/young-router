#!/usr/bin/env python3
"""Pin LiteLLM to the latest stable PyPI release before a desktop build."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = Path(os.environ.get("LITELLM_VERSION_FILE", ROOT / "LITELLM_VERSION"))
PYPI_JSON_URL = os.environ.get("LITELLM_PYPI_JSON_URL", "https://pypi.org/pypi/litellm/json")
RUNTIME_WHEEL_TAG = "cp312"


def stable_version(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    return tuple(int(part) for part in match.groups()) if match else None


def has_installable_artifact(files: object) -> bool:
    return isinstance(files, list) and any(
        isinstance(item, dict)
        and item.get("yanked") is not True
        and (
            item.get("packagetype") == "sdist"
            or (
                item.get("packagetype") == "bdist_wheel"
                and isinstance(item.get("filename"), str)
                and (
                    f"-{RUNTIME_WHEEL_TAG}-" in item["filename"]
                    or item["filename"].endswith("-py3-none-any.whl")
                )
            )
        )
        for item in files
    )


def latest_stable_version() -> str:
    request = urllib.request.Request(
        PYPI_JSON_URL,
        headers={"Accept": "application/json", "User-Agent": "litellm-menu-version-check"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    releases = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(releases, dict):
        raise RuntimeError("PyPI did not return LiteLLM release metadata")
    candidates = [
        (parsed, version)
        for version, files in releases.items()
        if (parsed := stable_version(version)) is not None and has_installable_artifact(files)
    ]
    if not candidates:
        raise RuntimeError("PyPI did not return an installable stable LiteLLM release")
    return max(candidates)[1]


def main(arguments: list[str]) -> int:
    if any(argument not in {"--check"} for argument in arguments) or len(arguments) > 1:
        print("usage: update-litellm.sh [--check]", file=sys.stderr)
        return 64
    latest = latest_stable_version()
    current = LOCK_FILE.read_text(encoding="utf-8").strip() if LOCK_FILE.exists() else ""
    if arguments == ["--check"]:
        if current != latest:
            print(f"LiteLLM lock is stale: locked={current or 'missing'}, latest={latest}", file=sys.stderr)
            return 1
        print(f"LiteLLM lock is current: {latest}")
        return 0
    LOCK_FILE.write_text(f"{latest}\n", encoding="utf-8")
    print(f"Updated LiteLLM lock: {current or 'missing'} -> {latest}")
    print("The current desktop build will package this exact LiteLLM release.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
