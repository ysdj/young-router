"""A relay's zero must not erase a real cache count.

LiteLLM's ``Usage`` fills ``prompt_tokens_details.cached_tokens`` from the
OpenAI-shaped field and then applies its Anthropic mapping, which assigns
``cache_read_input_tokens`` over it. The DeepSeek route this app forwards answers
both keys on the same frame -- the real count in ``prompt_tokens_details`` and a
literal ``0`` in ``cache_read_input_tokens`` -- so the real number was replaced
by zero before anything downstream could read it.

Measured on a real request, same body each time: straight to the upstream
``cached=29824``; through the proxy it arrived at Codex as ``cached_tokens: 0``.
Codex reads that number for its turn meter and its compaction decision, so the
zero made a cached thread look like a re-read one.
"""

from __future__ import annotations

import unittest

from litellm.types.utils import Usage

from young_router.proxy import patches


class UsageCacheOverwriteTests(unittest.TestCase):
    def setUp(self) -> None:
        patches._install_usage_cache_read_no_overwrite_patch()

    def test_the_relay_shape_keeps_the_real_count(self) -> None:
        usage = Usage(
            prompt_tokens=30_007,
            completion_tokens=1,
            total_tokens=30_008,
            prompt_tokens_details={"cached_tokens": 29_824},
            prompt_cache_hit_tokens=29_824,
            cache_read_input_tokens=0,
        )

        self.assertEqual(usage.prompt_tokens_details.cached_tokens, 29_824)

    def test_a_larger_anthropic_count_still_wins(self) -> None:
        usage = Usage(
            prompt_tokens=10,
            completion_tokens=1,
            total_tokens=11,
            prompt_tokens_details={"cached_tokens": 3},
            cache_read_input_tokens=8,
        )

        self.assertEqual(usage.prompt_tokens_details.cached_tokens, 8)

    def test_no_cache_information_stays_empty(self) -> None:
        usage = Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11)

        details = getattr(usage, "prompt_tokens_details", None)
        self.assertIn(getattr(details, "cached_tokens", 0), (None, 0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
