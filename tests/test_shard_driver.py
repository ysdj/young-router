"""The shard driver runs the interpreter it was given, however many words it is.

``scripts/test.sh`` selects an interpreter that is sometimes a command rather
than a path: without the installed app or ``LITELLM_TEST_PYTHON`` it is
``uv run --with ... python``.  The driver once took a single ``--python`` word
and ``test.sh`` handed it the first one, so every shard ran ``uv -m unittest``
and the default gate failed on such a checkout without running a test.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts" / "test_shards.py"
# Two small modules, so the sharded branch is the one exercised.
MODULES = ("tests/test_api_base.py", "tests/test_claude_aliases.py")


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DRIVER), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class ShardDriverInterpreterTests(unittest.TestCase):
    def test_a_one_word_interpreter_still_runs(self) -> None:
        result = _run("--python", sys.executable, "--shards", "2", "--", *MODULES)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("across 2 shards", result.stdout)

    def test_every_word_of_a_command_reaches_the_shard(self) -> None:
        # ``-X utf8`` stands in for ``uv run --with ...``: words after the
        # first, one of them starting with a dash.
        words = (sys.executable, "-X", "utf8")
        result = _run(
            *(f"--python-arg={word}" for word in words), "--shards", "2", "--", *MODULES
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("across 2 shards", result.stdout)
        self.assertNotIn("(0 tests", result.stdout)

    def test_the_serial_run_is_the_verbose_one(self) -> None:
        result = _run("--python", sys.executable, "--shards", "1", "--", MODULES[0])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # unittest writes its per-test lines to stderr.
        self.assertIn(" ... ok", result.stderr)

    def test_the_gate_hands_over_the_whole_command(self) -> None:
        script = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")
        self.assertIn('interpreter+=("--python-arg=$word")', script)
        self.assertNotIn('--python "${TEST_COMMAND[0]}"', script)


if __name__ == "__main__":
    unittest.main()
