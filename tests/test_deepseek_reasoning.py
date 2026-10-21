from __future__ import annotations

import unittest

from young_router.proxy import deepseek_reasoning


class DeepSeekReasoningTests(unittest.TestCase):
    def test_deepseek_route_is_recognized(self) -> None:
        request = {
            "model": "openai/deepseek-v4.1-flash",
            "reasoning": {"effort": "max", "summary": "raw"},
        }

        self.assertTrue(deepseek_reasoning.raw_reasoning_requested(request))

    def test_responses_field_names_are_recognized(self) -> None:
        request = {
            "model": "deepseek-v4.1-flash",
            "reasoning_effort": "max",
            "model_reasoning_summary": "raw",
            "litellm_metadata": {"model_group": "deepseek-v4.1-flash"},
        }

        self.assertTrue(deepseek_reasoning.is_deepseek_request(request))

    def test_non_deepseek_route_is_not_recognized(self) -> None:
        gemini = {
            "model": "gemini-4-argon",
            "reasoning": {"effort": "high", "summary": "raw"},
        }
        deepseek_default = {
            "model": "deepseek-v4.1-flash",
            "reasoning": {"effort": "high"},
        }

        self.assertFalse(deepseek_reasoning.is_deepseek_request(gemini))
        self.assertTrue(deepseek_reasoning.is_deepseek_request(deepseek_default))

    def test_deepseek_reasoning_is_opt_in(self) -> None:
        summary_auto = {
            "model": "deepseek-v4.1-flash",
            "reasoning": {"effort": "high", "summary": "auto"},
        }
        summary_none = {
            "model": "deepseek-v4.1-flash",
            "reasoning": {"effort": "high", "summary": "none"},
        }

        self.assertFalse(deepseek_reasoning.raw_reasoning_requested(summary_auto))
        self.assertFalse(deepseek_reasoning.raw_reasoning_requested(summary_none))


if __name__ == "__main__":
    unittest.main()
