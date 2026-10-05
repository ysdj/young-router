#!/usr/bin/env python3
"""Stage the latest ``dsh-vision-router`` package for an artifact build.

The upstream project is https://github.com/ysr666/dsh-vision-router; its npm
distribution owns the free OVH fallback chain, the local Ollama/LM Studio
provider shapes, and the ``freeFallback``/``timeoutMs``/``maxTokens`` semantics
this application used to keep a hand-written Python mirror of.

Upstream is a Node module and the vision fallback runs inside the Python
LiteLLM process, so the package is not importable there.  It is therefore
staged into the Core bundle the same way ``pi-web-access``, TraceOne, and
``dsh-workbuddy-connect`` are, and ``young_router/adapters/dsh_vision_worker.mjs``
calls it through the bundled Node.js runtime.  This script resolves the npm
``latest`` dist-tag, installs the package plus the peer closure it needs, writes
the one stub its closure requires, and flattens the result into Core.  No
repository lock is written: a stale chain copy is never packaged.

Upstream declares ``@deepseek-ai/dsh-llm-deepseek`` as a peer, and that package
in turn declares ``@deepseek-ai/dsh-environment`` — which is not published to
the public registry.  Only ``environmentOf`` is reached, to read an ambient API
key out of the launching environment, and this build has no such environment.
``write_environment_shim`` writes that single function locally; see
``ENVIRONMENT_SHIM_REASON`` for why the stub is written here rather than
vendored into the repository.

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
    PACKAGE_VERSION_PATTERN,
    UpdateError,
    find_package_manager,
    flatten_npm_package,
    package_metadata,
    request_bytes,
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

#: Peers the staged chain functions transitively import.  They are installed
#: without version ranges: a range resolved at build time is what
#: ``package_metadata`` already pinned for the package itself, and a peer whose
#: own closure is unpublished must not silently resolve to a different major.
#: The list is the transitive closure of the three chain functions above, and
#: ``_verify_staged_chain`` fails the build when a release needs one more, so
#: this table is a floor rather than a guess.
REQUIRED_PEER_PACKAGES = (
    # dsh-vision-router's own peers.
    "@deepseek-ai/dsh-anonymous-user-id",
    "@deepseek-ai/dsh-llm-deepseek",
    "@deepseek-ai/dsh-storage-domain",
    "sharp",
    # dsh-llm-deepseek's peers, reached through lib/sharp-runtime.js.
    "@deepseek-ai/dsh-credentials",
    "@deepseek-ai/dsh-invariants",
    "@deepseek-ai/dsh-llm",
    "@deepseek-ai/dsh-settings",
    "@deepseek-ai/dsh-timeout",
    # dsh-llm's peers, and the plugin framework the whole @deepseek-ai/dsh-*
    # family declares against.
    "@deepseek-ai/cordis",
    "@deepseek-ai/dsh-brand",
    "@deepseek-ai/dsh-home-paths",
    "@deepseek-ai/dsh-storage",
)

#: The unpublished peer, and the one function the staged closure reaches.
ENVIRONMENT_SHIM_PACKAGE = "@deepseek-ai/dsh-environment"
ENVIRONMENT_SHIM_ENTRY = "index.js"
ENVIRONMENT_SHIM_SOURCE = """\
// Written by scripts/update_dsh_vision_router.py — do not edit in place.
//
// @deepseek-ai/dsh-llm-deepseek declares @deepseek-ai/dsh-environment as a
// required peer, but that package is not published to the public registry, so
// the import below cannot be resolved from npm. The staged vision chain only
// calls environmentOf() to read an ambient API key out of the launching
// environment; Young Router supplies every credential through its own provider
// config and has no such ambient key, so a lookup that finds nothing is the
// faithful answer. If a future upstream release reads anything else from this
// module, this stub is wrong and the build must fail rather than serve a
// silent divergence — update_dsh_vision_router.py re-checks the call site.
export function environmentOf() {
  return {
    get() {
      return undefined
    },
  }
}
"""
ENVIRONMENT_SHIM_REASON = (
    f"{ENVIRONMENT_SHIM_PACKAGE} is required by dsh-llm-deepseek but is not "
    "published to the public npm registry"
)


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


def _peer_specs() -> list[str]:
    """Install the peer closure the staged chain functions import at load."""

    return list(REQUIRED_PEER_PACKAGES)


def _environment_shim_target(npm_root: Path) -> Path:
    return npm_root / "node_modules" / ENVIRONMENT_SHIM_PACKAGE / ENVIRONMENT_SHIM_ENTRY


def write_environment_shim(npm_root: Path) -> Path:
    """Write the one stub the staged package closure cannot resolve from npm.

    The stub is written into the temporary install tree before flattening, so
    the Core bundle carries it exactly as it carries every other staged
    dependency and no third-party file is ever patched in the repository.
    """

    target = _environment_shim_target(npm_root)
    scope = target.parent
    # pnpm only materialises a scope directory when a package occupies it, and
    # this one is never installed because it is not published.  A package
    # manager that *did* place a real copy here would mean the upstream
    # closure changed and this stub must not shadow it.
    if scope.exists() and any(scope.iterdir()):
        raise UpdateError(
            f"The staged {PACKAGE_NAME} install now carries a real {ENVIRONMENT_SHIM_PACKAGE}; "
            "the published package no longer needs the local stub — remove it and re-check "
            "ENVIRONMENT_SHIM_SOURCE"
        )
    scope.mkdir(parents=True, exist_ok=True)
    target.write_text(ENVIRONMENT_SHIM_SOURCE, encoding="utf-8")
    manifest = target.parent / "package.json"
    manifest.write_text(
        json.dumps(
            {
                "name": ENVIRONMENT_SHIM_PACKAGE,
                "version": "0.0.0-young-router-shim",
                "type": "module",
                "main": ENVIRONMENT_SHIM_ENTRY,
                "exports": {".": f"./{ENVIRONMENT_SHIM_ENTRY}"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _shim_reaches_only_environment_of(npm_root: Path) -> None:
    """Prove the staged closure still only needs ``environmentOf`` from the stub.

    A future upstream release could read another export from the unpublished
    module. Serving a stub that answers a different question would be a silent
    divergence, so the build fails here instead.
    """

    offenders: set[str] = set()
    for path in sorted((npm_root / "node_modules").rglob("*.js")):
        if ENVIRONMENT_SHIM_PACKAGE in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if ENVIRONMENT_SHIM_PACKAGE not in line:
                continue
            if "import" not in line and "require" not in line:
                continue
            for match in _imported_names(line):
                if match != "environmentOf":
                    offenders.add(match)
    if offenders:
        names = ", ".join(sorted(offenders))
        raise UpdateError(
            f"The staged {PACKAGE_NAME} closure imports {names} from "
            f"{ENVIRONMENT_SHIM_PACKAGE}, which the local stub does not provide; "
            "re-check the upstream release and update ENVIRONMENT_SHIM_SOURCE"
        )


def _imported_names(line: str) -> list[str]:
    import re

    names: list[str] = []
    brace = re.search(r"import\s*\{([^}]*)\}\s*from", line)
    if brace:
        names.extend(
            part.strip().split(" as ")[0].strip()
            for part in brace.group(1).split(",")
            if part.strip()
        )
    single = re.search(r"import\s+(\w+)\s+from", line)
    if single:
        names.append(single.group(1))
    namespace = re.search(r"import\s+\*\s+as\s+(\w+)\s+from", line)
    if namespace:
        names.append(namespace.group(1))
    return names


def _verify_staged_chain(npm_root: Path) -> dict[str, Any]:
    """Run the staged chain functions once with the bundled Node.js runtime.

    Importing them is the only proof that the peer closure, the shim, and the
    private entry path all resolve. The values are returned so a caller can
    compare them with the fallback the Core keeps.
    """

    import subprocess

    node = os.environ.get("YOUNG_ROUTER_NODE_BIN", "").strip() or _find_node()
    entry = npm_root / "node_modules" / PACKAGE_NAME / "lib" / "core-primitives.js"
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
            [node, "--input-type=module", "-e", script, str(entry)],
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
    latest, tarball_url, _version_payload = _package_metadata(registry_url)
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
            _peer_specs(),
            package_name=PACKAGE_NAME,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        write_environment_shim(npm_root)
        _shim_reaches_only_environment_of(npm_root)
        _verify_staged_chain(npm_root)
        package_version = flatten_npm_package(
            npm_root,
            output,
            package_name=PACKAGE_NAME,
            required_files=REQUIRED_PACKAGE_FILES,
        )
    if package_version != latest:
        raise UpdateError(f"The package manager installed {PACKAGE_NAME} {package_version}, expected {latest}")
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
