"""The streamed usage rebuild reads the cache spellings the request log does.

``response.completed`` is the only usage a streamed turn shows, and
``_responses_usage_for_codex`` rebuilds its ``input_tokens_details``.  The
request log counts the same turn's cache reads and writes through a wider set
of spellings; one this function skips makes the two surfaces disagree about
what a turn cost.
"""

from __future__ import annotations

from hook_test_utils import *


class ResponsesUsageCacheSpellingsTests(unittest.TestCase):
    def test_a_read_count_in_the_prompt_details_block_reaches_codex(self) -> None:
        hooks, _ = load_hook_module()

        usage = hooks._responses_usage_for_codex(
            {
                "input_tokens": 900,
                "output_tokens": 4,
                "input_tokens_details": {"cached_tokens": 0},
                "prompt_tokens_details": {"cache_read_tokens": 640},
            }
        )

        self.assertEqual(640, usage["input_tokens_details"]["cached_tokens"])

    def test_a_cache_hit_count_in_the_prompt_details_block_reaches_codex(self) -> None:
        hooks, _ = load_hook_module()

        usage = hooks._responses_usage_for_codex(
            {
                "input_tokens": 900,
                "output_tokens": 4,
                "input_tokens_details": {"cached_tokens": 0},
                "prompt_tokens_details": {"cache_hit_tokens": 512},
            }
        )

        self.assertEqual(512, usage["input_tokens_details"]["cached_tokens"])

    def test_a_top_level_write_count_reaches_codex(self) -> None:
        hooks, _ = load_hook_module()

        usage = hooks._responses_usage_for_codex(
            {
                "input_tokens": 500,
                "output_tokens": 8,
                "input_tokens_details": {"cached_tokens": 0},
                "cache_write_tokens": 480,
            }
        )

        self.assertEqual(480, usage["input_tokens_details"]["cache_write_tokens"])

    def test_a_write_count_in_the_prompt_details_block_reaches_codex(self) -> None:
        hooks, _ = load_hook_module()

        usage = hooks._responses_usage_for_codex(
            {
                "input_tokens": 500,
                "output_tokens": 8,
                "input_tokens_details": {"cached_tokens": 0},
                "prompt_tokens_details": {"cache_creation_tokens": 480},
            }
        )

        self.assertEqual(480, usage["input_tokens_details"]["cache_write_tokens"])
