#!/usr/bin/env python3
"""Run the unittest suite in shards, one process per shard.

The suite is 100+ modules and takes minutes in one interpreter on a machine with
ten cores.  Nothing in it needs to share a process: the modules are independent
by construction (every one of them is discovered on its own), so the only thing
a single run buys is the order, and the order is not a contract.

Shard assignment is greedy by *file size*, largest first.  Size is a crude
stand-in for cost, but it is the only measure available before running anything,
and it beats an alphabetical split by a wide margin: the biggest modules are an
order of magnitude larger than the median, and they are what decide the wall
clock once the small ones are spread out.

The driver is deliberately stdlib-only and runs on whatever ``python3`` the host
has, because the interpreter under test is the one that may be expensive to
start (a bundled runtime), not this.

Usage: test_shards.py --python <interpreter> [--shards N] -- <test files...>

Every shard prints one line on success; a failing shard prints its own log tail
and the driver exits non-zero with the failing shards named.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)


def plan(paths: list[Path], shards: int) -> list[list[Path]]:
    """Spread the largest modules first, each onto the lightest shard."""

    bins: list[list[Path]] = [[] for _ in range(shards)]
    load = [0] * shards
    for path in sorted(paths, key=lambda item: item.stat().st_size, reverse=True):
        target = load.index(min(load))
        bins[target].append(path)
        load[target] += path.stat().st_size
    return [sorted(bin_, key=lambda item: item.name) for bin_ in bins if bin_]


def module_name(path: Path) -> str:
    return path.stem


def run_shard(index: int, python: str, modules: list[Path], log: Path) -> tuple[int, str, float]:
    started = time.monotonic()
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join([str(REPO), str(REPO / "tests")])
    with log.open("w", encoding="utf-8") as handle:
        completed = subprocess.run(
            [python, "-m", "unittest", *[module_name(item) for item in modules]],
            cwd=REPO,
            env=environment,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return completed.returncode, completed.returncode and "failed" or "ok", time.monotonic() - started


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--shards", type=int, default=0)
    parser.add_argument("tests", nargs="+")
    args = parser.parse_args(argv)

    paths = [Path(item).resolve() for item in args.tests]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        print("missing test files: %s" % ", ".join(missing), file=sys.stderr)
        return 2

    shards = args.shards or min(8, os.cpu_count() or 4)
    shards = max(1, min(shards, len(paths)))
    if shards == 1:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join([str(REPO), str(REPO / "tests")])
        return subprocess.run(
            [args.python, "-m", "unittest", *[module_name(item) for item in paths]],
            cwd=REPO,
            env=environment,
            check=False,
        ).returncode

    bins = plan(paths, shards)
    workspace = Path(tempfile.mkdtemp(prefix="yr-tests-"))
    started = time.monotonic()
    results: list[tuple[int, int, str, float, Path, int]] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(bins)) as pool:
            futures = [
                pool.submit(run_shard, index, args.python, bin_, workspace / f"shard-{index + 1}.log")
                for index, bin_ in enumerate(bins)
            ]
            for index, (future, bin_) in enumerate(zip(futures, bins)):
                code, state, elapsed = future.result()
                results.append((index, code, state, elapsed, workspace / f"shard-{index + 1}.log", len(bin_)))
    finally:
        pass

    total_tests = 0
    failed: list[int] = []
    for index, code, state, elapsed, log, count in results:
        text = log.read_text(encoding="utf-8", errors="replace")
        found = RAN_RE.search(text)
        tests = int(found.group(1)) if found else 0
        total_tests += tests
        print(f"shard {index + 1}/{len(bins)}: {state} ({tests} tests, {count} modules, {elapsed:.0f}s)")
        if code != 0:
            failed.append(index + 1)
    elapsed = time.monotonic() - started

    if failed:
        for index, code, _state, _seconds, log, _count in results:
            if code == 0:
                continue
            print("", file=sys.stderr)
            print(f"--- shard {index + 1} failed: {log.name} ---", file=sys.stderr)
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-60:]:
                print(line, file=sys.stderr)
        print(
            f"\nFAILED: {total_tests} tests in {elapsed:.0f}s across {len(bins)} shards; "
            f"failing: {', '.join(map(str, failed))}. Logs kept in {workspace}",
            file=sys.stderr,
        )
        return 1

    shutil.rmtree(workspace, ignore_errors=True)
    print(f"\nOK: {total_tests} tests in {elapsed:.0f}s across {len(bins)} shards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
