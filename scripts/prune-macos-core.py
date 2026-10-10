#!/usr/bin/env python3
"""Remove development-only payload from the assembled macOS Core."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
import shutil


NODE_DEVELOPMENT_DIRS = {
    "test", "tests", "__tests__", "benchmark", "benchmarks", "coverage",
    "docs", "examples",
}
NODE_DEVELOPMENT_SUFFIXES = (
    ".d.ts", ".d.mts", ".d.cts", ".ts", ".mts", ".cts",
    ".js.map", ".mjs.map", ".cjs.map", ".ts.map", ".mts.map", ".cts.map",
    ".tsbuildinfo",
)
NODE_DOCUMENTATION_SUFFIXES = (".md", ".markdown", ".rst")
NODE_DEDUP_SUFFIXES = {
    ".js", ".cjs", ".mjs", ".json", ".css", ".html", ".wasm", ".png", ".txt",
}
OPTIONAL_RUNTIME_DIRS = {
    "polars", "_polars_runtime_32", "granian",
    "pyroscope", "pyroscope_io", "hf_xet",
    # LiteLLM's proxy extra installs these for optional admin/queue/MCP
    # surfaces. Young Router owns its native UI and never starts those
    # optional servers.
    "litellm_enterprise", "litellm_proxy_extras",
    "mcp", "mcp_types",
    "prompt_toolkit", "wcwidth",
    "rq", "croniter",
}
OPTIONAL_RUNTIME_DIST_PREFIXES = (
    "polars-", "polars_runtime_", "granian-",
    "pyroscope", "hf_xet-",
    "litellm_enterprise-", "litellm_proxy_extras-",
    "mcp-", "mcp-types-",
    "prompt_toolkit-", "wcwidth-",
    "rq-", "croniter-",
)


def prune_tree(root: Path, *, node: bool) -> None:
    # Do not follow dependency symlinks: all removals belong to this copied
    # bundle, never to a staging cache or package-manager store outside it.
    for directory, names, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in names[:]:
            target = parent / name
            if name in (NODE_DEVELOPMENT_DIRS if node else {"test", "tests"}):
                if target.is_symlink():
                    target.unlink()
                else:
                    shutil.rmtree(target)
                names.remove(name)
        if node:
            for name in files:
                if (
                    name.endswith(NODE_DEVELOPMENT_SUFFIXES)
                    or (
                        name.endswith(NODE_DOCUMENTATION_SUFFIXES)
                        and "node_modules" in parent.parts
                    )
                ) and "node_modules" in parent.parts:
                    (parent / name).unlink()
        elif "site-packages" in parent.parts:
            # Type stubs are consumed by type checkers, never by the bundled
            # interpreter. Keep runtime metadata and source modules intact.
            for name in files:
                if name.endswith(".pyi"):
                    (parent / name).unlink()


def deduplicate_node_assets(adapters: Path) -> tuple[int, int]:
    """Hardlink identical static dependency assets within the staged bundle.

    npm peer closures often contain byte-identical copies of the same SDK and
    schema files. Hardlinks preserve each path and its contents while APFS
    stores the shared bytes once. Native executables and Python files are
    excluded from this pass.
    """

    candidates: list[Path] = []
    for package in ("pi-web-access", "workbuddy-connect", "dsh-vision-router"):
        dependency_root = adapters / package / "node_modules"
        if dependency_root.is_dir():
            candidates.extend(
                path
                for path in dependency_root.rglob("*")
                if path.is_file() and not path.is_symlink()
                and path.suffix.lower() in NODE_DEDUP_SUFFIXES
                and path.stat().st_size >= 4096
            )

    by_size: dict[int, list[Path]] = {}
    for path in candidates:
        by_size.setdefault(path.stat().st_size, []).append(path)

    canonical: dict[tuple[int, int, bytes], Path] = {}
    linked_files = 0
    saved_bytes = 0
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        for path in paths:
            mode = stat.S_IMODE(path.stat().st_mode)
            digest = hashlib.sha256(path.read_bytes()).digest()
            key = (size, mode, digest)
            original = canonical.get(key)
            if original is None:
                canonical[key] = path
                continue
            path.unlink()
            os.link(original, path)
            linked_files += 1
            saved_bytes += size
    return linked_files, saved_bytes


def prune_core(core: Path) -> None:
    adapters = core / "young_router" / "adapters"
    for package in ("pi-web-access", "workbuddy-connect", "dsh-vision-router"):
        prune_tree(adapters / package, node=True)

    # The worker loads pi-web-access' TypeScript entry directly. Its generated
    # MCP CLI bundle is a separate npm executable, and the Pi SDK's optional
    # codemode/image helpers are not reached by this worker.
    shutil.rmtree(adapters / "pi-web-access" / "dist", ignore_errors=True)
    for package in ("esbuild", "quickjs-wasi", "temml", "@silvia-odwyer/photon-node"):
        shutil.rmtree(adapters / "pi-web-access" / "node_modules" / package, ignore_errors=True)
    shutil.rmtree(adapters / "pi-web-access" / "node_modules" / "@esbuild", ignore_errors=True)
    for node_modules in (
        adapters / "pi-web-access" / "node_modules",
        adapters / "workbuddy-connect" / "node_modules",
        adapters / "dsh-vision-router" / "node_modules",
    ):
        # Package-manager bookkeeping is useful only while staging; Node's
        # resolver needs the package.json files, not the install graph.
        for name in (".modules.yaml", ".package-map.json", ".pnpm-workspace-state-v1.json"):
            (node_modules / name).unlink(missing_ok=True)

    # These two assets demonstrate the extension on its npm page; the worker
    # loads index.ts and its resources, never the promotional image or video.
    for name in ("banner.png", "pi-web-fetch-demo.mp4"):
        (adapters / "pi-web-access" / name).unlink(missing_ok=True)
    linked_files, saved_bytes = deduplicate_node_assets(adapters)
    if linked_files:
        print(
            f"Deduplicated {linked_files} identical Node assets, sharing "
            f"{saved_bytes / (1024 * 1024):.1f} MiB.",
            flush=True,
        )

    prune_tree(core / "runtime" / "site-packages", node=False)
    # Python package tests are never imported by the router; their fixtures
    # are especially large in botocore and Pillow.
    for package in (core / "runtime" / "site-packages").iterdir():
        if package.is_dir() and package.name not in {"litellm"}:
            prune_tree(package, node=False)
    # The native host owns all windows. Python's development headers, package
    # installer and GUI tooling are not runtime inputs; keep the stdlib and
    # distribution metadata needed by importlib.metadata.
    python = core / "runtime" / "python"
    for relative in (
        "include", "share/man", "lib/pkgconfig",
        "lib/python3.12/ensurepip", "lib/python3.12/idlelib",
        "lib/python3.12/lib2to3", "lib/python3.12/pydoc_data",
        "lib/python3.12/tkinter", "lib/python3.12/turtledemo",
        "lib/python3.12/venv", "lib/python3.12/__phello__",
        "lib/tcl9.0", "lib/tk9.0", "lib/itcl4.3.5", "lib/thread3.0.4",
        "lib/python3.12/site-packages/pip",
        "lib/python3.12/site-packages/pip-26.1.1.dist-info",
    ):
        target = python / relative
        if target.is_dir():
            shutil.rmtree(target)
    for name in (
        "2to3", "2to3-3.12", "idle3", "idle3.12",
        "pydoc3", "pydoc3.12", "pip", "pip3", "pip3.12",
    ):
        (python / "bin" / name).unlink(missing_ok=True)

    # LiteLLM's proxy extra includes optional servers, profilers, the Polars
    # analytics engine and HuggingFace's Xet transfer accelerator. The router
    # runs its own uvicorn proxy, does not enable Pyroscope, and does not use
    # Focus/CloudZero/Polars analytics; removing these optional distributions
    # keeps the full HTTP provider path self-contained.
    site_packages = core / "runtime" / "site-packages"
    for name in OPTIONAL_RUNTIME_DIRS:
        target = site_packages / name
        if target.is_dir():
            shutil.rmtree(target)
    for target in site_packages.iterdir():
        if target.is_dir() and target.name.endswith(".dist-info") and any(
            target.name.startswith(prefix) for prefix in OPTIONAL_RUNTIME_DIST_PREFIXES
        ):
            shutil.rmtree(target)

    # Keep LiteLLM's import-time proxy dependencies, but drop package payload
    # that is only pulled in by optional CLI/admin integrations. Their import
    # records are retained where the interpreter needs them for package
    # discovery and the proxy smoke test above remains the authority.
    for package in ("pygments", "typer", "shellingham"):
        shutil.rmtree(site_packages / package, ignore_errors=True)
    for target in site_packages.iterdir():
        if target.is_dir() and target.name.endswith(".dist-info") and any(
            target.name.startswith(prefix) for prefix in (
                "Pygments-", "pygments-", "typer-", "shellingham-",
            )
        ):
            shutil.rmtree(target)

    # The Rust tokenizer backend is optional; LiteLLM's Python tokenizer and
    # tiktoken paths remain available when this native accelerator is absent.
    for native in (site_packages / "litellm" / "rust_bridge").glob("*.so"):
        native.unlink()

    # The native app never invokes LiteLLM's terminal admin client. Keep the
    # proxy launcher itself, but remove that client command tree.
    shutil.rmtree(site_packages / "litellm" / "proxy" / "client" / "cli", ignore_errors=True)

    # Swagger assets and the lazy OpenAPI snapshot only serve the optional web
    # admin surface. Keep an empty mount directory so proxy import remains
    # valid while those static bytes do not ship.
    swagger = site_packages / "litellm" / "proxy" / "swagger"
    if swagger.is_dir():
        for child in swagger.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
    else:
        swagger.mkdir(parents=True)
    (site_packages / "litellm" / "proxy" / "_lazy_openapi_snapshot.json").unlink(missing_ok=True)

    # LiteLLM bundles an admin web UI even though this app exposes its own
    # native settings surface. The proxy import tolerates a missing UI tree
    # and continues to serve the API routes.
    ui = site_packages / "litellm" / "proxy" / "_experimental" / "out"
    shutil.rmtree(ui, ignore_errors=True)
    # LiteLLM mounts this directory unconditionally during proxy import. A
    # tiny valid shell avoids noisy startup errors while the native app keeps
    # its own UI and does not ship the 15 MiB admin bundle.
    (ui / "_next").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><title>Young Router</title>\n", encoding="utf-8")

    # These are optional development/runtime support files left by the
    # standalone Python distribution after the GUI modules were removed.
    python = core / "runtime" / "python"
    shutil.rmtree(python / "lib" / "tcl9", ignore_errors=True)
    for name in ("libtcl9.0.dylib", "libtcl9tk9.0.dylib", "libtk9.0.dylib"):
        (python / "lib" / name).unlink(missing_ok=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("core", type=Path, help="assembled Contents/Resources/Core directory")
    prune_core(parser.parse_args().core)
