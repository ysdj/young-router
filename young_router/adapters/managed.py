"""Start the managed workers a proxy-only host routes through.

Wherever there is a Core, the Core owns these workers: it starts one when the
saved configuration names the base reference that worker publishes, hands the
resolved address and bearer to the proxy child, and releases the worker once no
route names it. A host that runs only the proxy -- a gateway fed a projected
config, with no Core and no window -- has the same routes and none of that
lifecycle. Every such deployment therefore used to grow its own copy of it: one
launcher per worker, each re-deriving a port, a bearer, and (for the free-model
lane) the hand-off descriptors the worker's start contract requires.

This module is that lifecycle without the Core, and it deliberately names no
worker. ``adapters.managed_worker_modules`` is the registry; a worker publishes
``API_BASE_ENV`` and a ``managed_runtime`` factory and is then started here on
the same terms the Core uses:

* only when the configuration carries one of its ``os.environ/`` references,
* never at the cost of the proxy -- a worker that cannot start loses its own
  routes and nothing else,
* as a child of the process that serves, so the worker's own parent watch ends
  it when the gateway is killed rather than leaving a holder on its ports.

Usage (a gateway launcher calls this instead of the proxy directly):

    python -m young_router.adapters.managed --config /opt/yr/config.yaml -- \\
        python -m young_router.proxy.macos_proxy --config /opt/yr/config.yaml ...

The command is executed in this process with the workers' fragment in its
environment; there is no second supervisor and no file to read back.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

from .. import config_paths
from . import managed_worker_modules


def _config_text(config_path: Path) -> str:
    """The document whose references decide which workers start.

    Read as text rather than parsed: the question is only "is this spelling
    present", and a document LiteLLM accepts must never fail to start a worker
    because a parser here disagreed with it.
    """

    try:
        return config_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _references(module: Any) -> frozenset[str]:
    names = getattr(module, "API_BASE_ENV", None)
    if not isinstance(names, Mapping):
        return frozenset()
    return frozenset(f"os.environ/{name}" for name in names.values() if isinstance(name, str) and name)


def environment(
    config_path: Path | str,
    *,
    autostart: bool = True,
    modules: Iterable[Any] | None = None,
) -> dict[str, str]:
    """The environment fragment every worker this configuration names publishes.

    ``autostart`` is the gateway's path: the workers are started here and are
    not expected to be running already. Without it the answer is whatever the
    running workers last published, which is what a status read wants and what
    must never spawn a process on its own.
    """

    text = _config_text(Path(config_path).expanduser())
    root = config_paths.runtime_root()
    fragment: dict[str, str] = {}
    for module in modules if modules is not None else managed_worker_modules():
        references = _references(module)
        if not references or not any(reference in text for reference in references):
            continue
        # A package this host never staged is a route it cannot serve, not a
        # reason for the proxy to refuse every other route.
        try:
            if not module.available():
                continue
            factory = getattr(module, "managed_runtime", None)
            if factory is None:
                continue
            runtime = factory(root)
            fragment.update(runtime.environment(autostart=autostart))
        except Exception:
            continue
    return fragment


def _parse_arguments(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="python -m young_router.adapters.managed",
        description="Start the managed workers a configuration references, then exec the proxy.",
    )
    parser.add_argument("--config", default=str(config_paths.provider_config_path()))
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="publish only what already-running workers report; never spawn one",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="the command to exec with the fragment in its environment (after --)",
    )
    arguments = parser.parse_args(argv)
    command = list(arguments.command)
    if command[:1] == ["--"]:
        command = command[1:]
    return arguments, command


def main(argv: list[str] | None = None) -> int:
    arguments, command = _parse_arguments(list(sys.argv[1:] if argv is None else argv))
    fragment = environment(arguments.config, autostart=not arguments.no_start)
    # The values are bearer secrets: report the variables, never what they hold.
    print(f"managed workers: {', '.join(sorted(fragment)) or 'none'}", file=sys.stderr)
    if not command:
        print(json.dumps(sorted(fragment)), file=sys.stderr)
        return 0
    os.environ.update(fragment)
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        print(f"could not exec {command[0]}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
