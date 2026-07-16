from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "mac_menu" / "Sources" / "ProbeProtocolOrder.swift"


HARNESS = r'''
import Foundation

let priority = ["openai/responses", "openai/chat", "anthropic"]
let available = Array(CommandLine.arguments.dropFirst())
let recommendation = probeProtocolRecommendation(
    priority: priority,
    availableModes: available
)
print(recommendation.supported.joined(separator: ","))
print(recommendation.displayOrder.joined(separator: ","))
'''


class ProbeProtocolOrderSwiftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        temp = Path(cls.temp.name)
        harness = temp / "main.swift"
        harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
        cls.binary = temp / "probe-protocol-order"
        result = subprocess.run(
            ["swiftc", str(HELPER), str(harness), "-o", str(cls.binary)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def recommendation(self, *available: str) -> tuple[list[str], list[str]]:
        result = subprocess.run(
            [str(self.binary), *available],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        return (
            lines[0].split(",") if lines[0] else [],
            lines[1].split(",") if lines[1] else [],
        )

    def test_all_available_protocols_are_saved_in_fallback_priority(self) -> None:
        supported, display_order = self.recommendation(
            "anthropic", "openai/chat", "openai/responses"
        )
        self.assertEqual(
            supported,
            ["openai/responses", "openai/chat", "anthropic"],
        )
        self.assertEqual(display_order, supported)

    def test_unavailable_protocols_follow_the_saved_fallbacks(self) -> None:
        supported, display_order = self.recommendation("anthropic", "openai/chat")
        self.assertEqual(supported, ["openai/chat", "anthropic"])
        self.assertEqual(
            display_order,
            ["openai/chat", "anthropic", "openai/responses"],
        )


if __name__ == "__main__":
    unittest.main()
