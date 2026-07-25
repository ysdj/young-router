from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import browser_billing  # noqa: E402


class BrowserBillingTests(unittest.TestCase):
    def test_origin_strips_api_path_and_rejects_credentials(self) -> None:
        self.assertEqual(
            "https://billing.example.test",
            browser_billing._service_origin("https://billing.example.test/api/v1"),
        )
        self.assertIsNone(browser_billing._service_origin("https://u:p@billing.example.test/v1"))
        self.assertIsNone(browser_billing._service_origin("not-a-url"))

    def test_apple_script_quotes_token_as_javascript_literal(self) -> None:
        script = browser_billing._apple_script(
            "https://billing.example.test",
            'key"quoted\\value',
            "Google Chrome",
        )
        self.assertIn("const origin = \\\"https://billing.example.test\\\"", script)
        self.assertIn('const token = \\\"key\\\\\\\"quoted', script)
        self.assertNotIn("%TOKEN%", script)
        self.assertIn("No existing browser tab matches the provider origin", script)
        self.assertNotIn("make new tab", script)
        self.assertNotIn("close workingTab", script)
        self.assertNotIn("targetPage", script)
        self.assertIn("on isSameOriginPage", script)
        self.assertIn('candidateURL starts with (targetOrigin & "/")', script)
        self.assertNotIn("candidateURL starts with targetOrigin then", script)
        # The current bridge passes only the origin as the script argument; the
        # API key stays in stdin and never enters the process argument vector.
        self.assertIn("item 1 of argv", script)
        self.assertIn("safeFields", script)
        self.assertIn("The browser is not running", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("New-Api-User", script)
        self.assertNotIn("document.cookie", script)

        expression = browser_billing._page_expression(
            "https://billing.example.test", "synthetic-key"
        )
        self.assertEqual(
            (
                "status",
                "remain_quota",
                "unlimited_quota",
                "used_quota",
                "total_granted",
                "group",
                "__browser_multiplier",
                "__browser_match",
            ),
            browser_billing._SAFE_ITEM_FIELDS,
        )
        self.assertIn('const safeFields = ["status", "remain_quota"', expression)
        self.assertNotIn('"id", "user_id"', expression)
        self.assertNotIn("message: typeof payload.message", expression)

    def test_bridge_parses_json_and_does_not_emit_key(self) -> None:
        seen: list[list[str]] = []

        class Result:
            returncode = 0
            stdout = json.dumps({"data": {"items": [{"key": "masked", "remain_quota": 12.5}]}})
            stderr = ""

        def runner(command: list[str], **_: object) -> Result:
            seen.append(command)
            return Result()

        status, http_status, payload = browser_billing.fetch_browser_token_search(
            "https://billing.example.test/v1",
            "synthetic-key",
            runner=runner,
        )
        self.assertEqual(("ok", 200), (status, http_status))
        self.assertEqual(12.5, payload["data"]["items"][0]["remain_quota"])
        self.assertNotIn("synthetic-key", json.dumps(payload))
        self.assertEqual("osascript", seen[0][0])
        self.assertNotIn("synthetic-key", " ".join(seen[0]))
        self.assertEqual("https://billing.example.test", seen[0][-1])

    def test_bridge_classifies_timeout_and_disabled_javascript(self) -> None:
        def timeout_runner(*_: object, **__: object) -> object:
            raise subprocess.TimeoutExpired("osascript", 1)

        self.assertEqual(
            ("timeout", None, None),
            browser_billing.fetch_browser_token_search(
                "https://billing.example.test/v1", "secret", runner=timeout_runner
            ),
        )

        class Failed:
            returncode = 1
            stdout = ""
            stderr = "Apple events JavaScript disabled"

        self.assertEqual(
            ("unavailable", None, None),
            browser_billing.fetch_browser_token_search(
                "https://billing.example.test/v1",
                "secret",
                runner=lambda *_args, **_kwargs: Failed(),
            ),
        )

    def test_bridge_classifies_missing_existing_tab(self) -> None:
        class Failed:
            returncode = 1
            stdout = ""
            stderr = "No existing browser tab matches the provider origin"

        self.assertEqual(
            ("unavailable", None, None),
            browser_billing.fetch_browser_token_search(
                "https://billing.example.test/v1",
                "secret",
                runner=lambda *_args, **_kwargs: Failed(),
            ),
        )


if __name__ == "__main__":
    unittest.main()
