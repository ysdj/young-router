#!/usr/bin/env python3
"""Stage the latest ``dsh-vision-router`` package for an artifact build.

The upstream project is https://github.com/ysr666/dsh-vision-router; its npm
distribution owns the free OVH fallback chain, the local Ollama/LM Studio
provider shapes, and the ``freeFallback``/``timeoutMs``/``maxTokens`` semantics
this application used to keep a hand-written Python mirror of.

Upstream is a Node module and the vision fallback runs inside the Python
LiteLLM process, so the package is not importable there.  It is therefore
staged into the Core bundle the same way modelprint and
``dsh-connect-workbuddy`` are, and ``young_router/adapters/dsh_vision_worker.mjs``
calls it through the bundled Node.js runtime. This script resolves the npm
``latest`` dist-tag, installs its package and dependencies, and flattens the
result into Core. The worker uses the provider lists, which do not need the
optional DeepSeek Harness and Sharp peers.

``*_URL`` environment variables and the matching command-line options exist for
offline/unit-test fixtures.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from update_common import (
    UpdateError,
    find_package_manager,
    flatten_npm_package,
    package_metadata,
    record_staged_release,
    release_integrity,
    request_bytes,
    reused_staged_release,
    run_package_manager_install,
)


PACKAGE_NAME = "dsh-vision-router"
PACKAGE_REGISTRY_URL = os.environ.get(
    "DSH_VISION_ROUTER_NPM_REGISTRY_URL",
    f"https://registry.npmjs.org/{PACKAGE_NAME}",
)
DEFAULT_TIMEOUT_SECONDS = 180
# Build-tool metadata lookup identity: this script runs on a maintainer's
# machine, not in the shipped app.
USER_AGENT = "Young-Router/dsh-vision-router-build"

#: Entry point the worker imports.  The package's own ``exports`` map does not
#: publish this subpath, so the worker resolves it by absolute file path; this
#: entry is verified after staging so an upstream move fails the build.
REQUIRED_PACKAGE_FILES = ("package.json", "lib/core-primitives.js")


def _request_bytes(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    return request_bytes(url, timeout=timeout, user_agent=USER_AGENT)


def _package_metadata(registry_url: str) -> tuple[str, str, dict[str, Any]]:
    """Resolve the ``latest`` dist-tag and its tarball from the npm registry."""

    return package_metadata(
        registry_url,
        package_name=PACKAGE_NAME,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        user_agent=USER_AGENT,
    )


def _verify_staged_chain(npm_root: Path) -> dict[str, Any]:
    """Run the staged chain functions once with the bundled Node.js runtime.

    This exercises the private entry point the worker uses against the installed
    dependencies.
    """

    import subprocess

    node = os.environ.get("YOUNG_ROUTER_NODE_BIN", "").strip() or _find_node()
    entry = npm_root / "node_modules" / PACKAGE_NAME / "lib" / "core-primitives.js"
    # ``import()`` resolves a specifier, not a filesystem path, so a raw
    # Windows path is rejected outright: "Only URLs with a scheme in: file,
    # data, and node are supported by the default ESM loader. On Windows,
    # absolute paths must be valid file:// URLs." Passing the URL form works
    # on every host, because a POSIX path is already a valid specifier.
    entry_url = entry.resolve().as_uri()
    script = (
        "import(process.argv[1]).then((m) => {"
        "  const config = { freeFallback: true, httpProviders: [], providers: [] };"
        "  const out = {"
        "    free: m.httpProvidersOf(config, true).map((p) => [p.name, p.baseURL, p.model]),"
        "    ollama: m.localOllamaProvidersOf({ localOllama: { enabled: true } }),"
        "    lmstudio: m.localLmStudioProvidersOf({ localLmStudio: { enabled: true } })"
        "  };"
        "  process.stdout.write(JSON.stringify(out));"
        "}).catch((e) => { process.stderr.write(String(e && e.message || e)); process.exit(1); });"
    )
    try:
        result = subprocess.run(
            [node, "--input-type=module", "-e", script, entry_url],
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"Could not run the staged {PACKAGE_NAME} chain: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()[-2000:]
        raise UpdateError(
            f"The staged {PACKAGE_NAME} chain could not be loaded, so the Core bundle "
            f"would ship an unusable vision fallback: {details}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UpdateError(f"The staged {PACKAGE_NAME} chain returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("free"), list):
        raise UpdateError(f"The staged {PACKAGE_NAME} chain returned no free provider list")
    return payload


def _find_node() -> str:
    import shutil

    node = shutil.which("node")
    if not node:
        raise UpdateError(
            "Node.js is required to verify the staged dsh-vision-router chain; "
            "set YOUNG_ROUTER_NODE_BIN to its executable path"
        )
    return node


def update(output: Path, *, registry_url: str) -> str:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    latest, tarball_url, version_payload = _package_metadata(registry_url)
    integrity = release_integrity(version_payload)
    # The release was resolved above on this build; this only skips installing
    # the identical one again.  The staged chain was proved importable when it
    # was staged, and the build proves it again on the assembled bundle.
    if reused_staged_release(
        output,
        package_name=PACKAGE_NAME,
        version=latest,
        integrity=integrity,
        required_files=REQUIRED_PACKAGE_FILES,
    ):
        return latest
    manager, manager_env = find_package_manager(
        "DSH_VISION_ROUTER_PACKAGE_MANAGER_BIN", purpose=f"stage {PACKAGE_NAME}"
    )
    from update_common import temporary_install_tree

    with temporary_install_tree(f"young-router-{PACKAGE_NAME}-") as work:
        package_tarball = work / f"{PACKAGE_NAME}.tgz"
        package_tarball.write_bytes(_request_bytes(tarball_url))
        npm_root = work / "install"
        run_package_manager_install(
            manager,
            manager_env,
            npm_root,
            package_tarball,
            (),
            package_name=PACKAGE_NAME,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        _verify_staged_chain(npm_root)
        package_version = flatten_npm_package(
            npm_root,
            output,
            package_name=PACKAGE_NAME,
            required_files=REQUIRED_PACKAGE_FILES,
        )
    if package_version != latest:
        raise UpdateError(f"The package manager installed {PACKAGE_NAME} {package_version}, expected {latest}")
    record_staged_release(
        output, package_name=PACKAGE_NAME, version=latest, integrity=integrity
    )
    return package_version


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Core/young_router/adapters/dsh-vision-router destination")
    parser.add_argument("--registry-url", default=PACKAGE_REGISTRY_URL)
    return parser.parse_args(arguments)


def main(arguments: list[str]) -> int:
    options = parse_arguments(arguments)
    try:
        package_version = update(Path(options.output), registry_url=options.registry_url)
    except (OSError, UpdateError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Installed {PACKAGE_NAME} {package_version} into {Path(options.output).expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
