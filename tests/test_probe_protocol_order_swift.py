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

let arguments = Array(CommandLine.arguments.dropFirst())
let modelIdentifier = arguments.first ?? ""
let defaultPriority = ["openai/responses", "openai/chat", "anthropic"]
let priority = probeProtocolPriority(
    modelIdentifier: modelIdentifier,
    defaultPriority: defaultPriority
)
let available = Array(arguments.dropFirst())
let recommendation = probeProtocolRecommendation(
    priority: priority,
    availableModes: available
)
print(inferredPreferredUpstreamApiMode(
    modelIdentifier: modelIdentifier,
    defaultMode: defaultPriority[0]
))
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

    def recommendation(
        self, model_identifier: str, *available: str
    ) -> tuple[str, list[str], list[str]]:
        result = subprocess.run(
            [str(self.binary), model_identifier, *available],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 3)
        return (
            lines[0],
            lines[1].split(",") if lines[1] else [],
            lines[2].split(",") if lines[2] else [],
        )

    def test_all_available_protocols_select_only_the_best_protocol(self) -> None:
        inferred, supported, display_order = self.recommendation(
            "generic-chat",
            "anthropic", "openai/chat", "openai/responses"
        )
        self.assertEqual(inferred, "openai/responses")
        self.assertEqual(supported, ["openai/responses"])
        self.assertEqual(
            display_order,
            ["openai/responses", "openai/chat", "anthropic"],
        )

    def test_unavailable_protocols_follow_the_saved_fallbacks(self) -> None:
        _, supported, display_order = self.recommendation(
            "generic-chat", "anthropic", "openai/chat"
        )
        self.assertEqual(supported, ["openai/chat"])
        self.assertEqual(
            display_order,
            ["openai/chat", "anthropic", "openai/responses"],
        )

    def test_claude_prefers_anthropic_when_multiple_protocols_are_available(self) -> None:
        inferred, supported, display_order = self.recommendation(
            "claude-example-5",
            "openai/responses",
            "openai/chat",
            "anthropic",
        )
        self.assertEqual(inferred, "anthropic")
        self.assertEqual(supported, ["anthropic"])
        self.assertEqual(
            display_order,
            ["anthropic", "openai/responses", "openai/chat"],
        )

    def test_probe_availability_overrides_the_claude_name_hint(self) -> None:
        inferred, supported, display_order = self.recommendation(
            "anthropic.claude-example-v1",
            "openai/responses",
            "openai/chat",
        )
        self.assertEqual(inferred, "anthropic")
        self.assertEqual(supported, ["openai/responses"])
        self.assertEqual(
            display_order,
            ["openai/responses", "openai/chat", "anthropic"],
        )

    def test_claude_must_be_a_complete_model_identifier_token(self) -> None:
        inferred, supported, _ = self.recommendation(
            "notclaude-proxy", "anthropic", "openai/responses"
        )
        self.assertEqual(inferred, "openai/responses")
        self.assertEqual(supported, ["openai/responses"])


if __name__ == "__main__":
    unittest.main()
