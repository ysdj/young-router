#!/usr/bin/env python3
"""Remove development and optional payload from an assembled Windows Core."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
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

# These are optional LiteLLM proxy/admin/analytics integrations. The Core owns
# the API process and native UI; it never starts the admin UI, queue workers,
# profilers, Polars analytics, or the terminal client.
OPTIONAL_RUNTIME_DIRS = {
    "_polars_runtime_32", "polars", "polars_runtime_32", "granian",
    "pyroscope", "pyroscope_io", "hf_xet", "litellm_enterprise",
    "litellm_proxy_extras", "prompt_toolkit", "wcwidth", "rq", "croniter",
    "pip", "setuptools", "wheel",
}
OPTIONAL_RUNTIME_DIST_PREFIXES = (
    "_polars_runtime_32-", "polars-", "polars_runtime_", "granian-",
    "pyroscope", "hf_xet-", "litellm_enterprise-", "litellm_proxy_extras-",
    "prompt_toolkit-", "wcwidth-", "rq-", "croniter-", "pip-", "setuptools-",
    "wheel-",
)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def prune_node_tree(root: Path) -> None:
    if not root.is_dir():
        return
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        parent = Path(directory)
        for name in names[:]:
            target = parent / name
            if name in NODE_DEVELOPMENT_DIRS:
                _remove(target)
                names.remove(name)
        if "node_modules" in parent.parts:
            for name in files:
                target = parent / name
                if name.endswith(NODE_DEVELOPMENT_SUFFIXES) or name.endswith(NODE_DOCUMENTATION_SUFFIXES):
                    target.unlink(missing_ok=True)
    for name in (".modules.yaml", ".package-map.json", ".pnpm-workspace-state-v1.json"):
        (root / "node_modules" / name).unlink(missing_ok=True)


def prune_python_runtime(runtime: Path) -> None:
    site = runtime / "Lib" / "site-packages"
    if not site.is_dir():
        return

    for directory, names, files in os.walk(site, topdown=True, followlinks=False):
        parent = Path(directory)
        for name in names[:]:
            target = parent / name
            if name in {"test", "tests", "__pycache__"}:
                _remove(target)
                names.remove(name)
        for name in files:
            if name.endswith((".pyc", ".pyo", ".pyi")):
                (parent / name).unlink(missing_ok=True)

    for name in OPTIONAL_RUNTIME_DIRS:
        _remove(site / name)
    for entry in site.iterdir():
        if entry.is_dir() and entry.name.endswith(".dist-info") and any(
            entry.name.startswith(prefix) for prefix in OPTIONAL_RUNTIME_DIST_PREFIXES
        ):
            _remove(entry)

    # LiteLLM's optional web/admin payload is not used by the desktop app.
    shutil.rmtree(site / "litellm" / "proxy" / "client" / "cli", ignore_errors=True)
    (site / "litellm" / "proxy" / "_lazy_openapi_snapshot.json").unlink(missing_ok=True)
    swagger = site / "litellm" / "proxy" / "swagger"
    if swagger.is_dir():
        for child in swagger.iterdir():
            _remove(child)
    admin_ui = site / "litellm" / "proxy" / "_experimental" / "out"
    shutil.rmtree(admin_ui, ignore_errors=True)

    # CPython's installer and GUI/development libraries are not runtime inputs.
    for relative in (
        "ensurepip", "idlelib", "lib2to3", "pydoc_data", "tkinter", "turtledemo",
        "venv", "__phello__",
    ):
        _remove(runtime / "Lib" / relative)
    _remove(runtime / "Scripts")
    for directory, names, files in os.walk(runtime, topdown=True, followlinks=False):
        parent = Path(directory)
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                (parent / name).unlink(missing_ok=True)


def prune_core(core: Path) -> None:
    adapters = core / "young_router" / "adapters"
    for package in ("connect-workbuddy", "dsh-vision-router", "our-free-model"):
        prune_node_tree(adapters / package)
    prune_python_runtime(core / "runtime" / "bin")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("core", nargs="?", type=Path)
    parser.add_argument("--runtime-bin", type=Path)
    options = parser.parse_args()
    if options.runtime_bin is not None:
        prune_python_runtime(options.runtime_bin.expanduser().resolve())
    elif options.core is not None:
        prune_core(options.core.expanduser().resolve())
    else:
        parser.error("provide a Core directory or --runtime-bin")
