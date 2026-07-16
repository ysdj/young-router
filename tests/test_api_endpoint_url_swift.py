from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "mac_menu" / "Sources" / "APIEndpointURL.swift"


HARNESS = r'''
import Foundation

for raw in CommandLine.arguments.dropFirst() {
    let values = raw.split(separator: "|", maxSplits: 1, omittingEmptySubsequences: false)
    guard values.count == 2 else { exit(2) }
    let urls = apiEndpointURLCandidates(baseURL: String(values[0]), endpoint: String(values[1]))
    print(urls.map(\.absoluteString).joined(separator: ","))
}
'''


class APIEndpointURLSwiftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        temp = Path(cls.temp.name)
        harness = temp / "main.swift"
        harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
        cls.binary = temp / "api-endpoint-url-helper"
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

    def candidates(self, base_url: str, endpoint: str) -> list[str]:
        result = subprocess.run(
            [str(self.binary), f"{base_url}|{endpoint}"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout.strip()
        return output.split(",") if output else []

    def test_fetch_models_handles_all_supported_base_url_shapes(self) -> None:
        cases = {
            "https://api.example.test": [
                "https://api.example.test/v1/models",
                "https://api.example.test/models",
            ],
            "api.example.test": [
                "https://api.example.test/v1/models",
                "https://api.example.test/models",
            ],
            "https://api.example.test/": [
                "https://api.example.test/v1/models",
                "https://api.example.test/models",
            ],
            "https://api.example.test/v1": ["https://api.example.test/v1/models"],
            "https://api.example.test/v1/": ["https://api.example.test/v1/models"],
            "https://api.example.test/v1/chat/completions": ["https://api.example.test/v1/models"],
            "https://api.example.test/chat/completions/": [
                "https://api.example.test/models",
                "https://api.example.test/v1/models",
            ],
            "https://api.example.test/v1/messages": ["https://api.example.test/v1/models"],
            "https://api.example.test/v1/completion": ["https://api.example.test/v1/models"],
        }
        for base_url, expected in cases.items():
            with self.subTest(base_url=base_url):
                self.assertEqual(self.candidates(base_url, "models"), expected)

    def test_probe_endpoints_replace_a_supplied_full_endpoint(self) -> None:
        self.assertEqual(
            self.candidates("https://api.example.test/v1/messages", "chat/completions"),
            ["https://api.example.test/v1/chat/completions"],
        )
        self.assertEqual(
            self.candidates("https://api.example.test/v1/chat/completions", "messages"),
            ["https://api.example.test/v1/messages"],
        )
        self.assertEqual(
            self.candidates("https://api.example.test/v1/completion", "responses"),
            ["https://api.example.test/v1/responses"],
        )


if __name__ == "__main__":
    unittest.main()
