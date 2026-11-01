"""The bridged Responses shape must carry the provider's cache-hit count.

A relay reports the same fact under its own name: DeepSeek answers
``prompt_cache_hit_tokens`` and ``cache_read_input_tokens`` while leaving
``prompt_tokens_details.cached_tokens`` at zero. LiteLLM copies only the latter
into the Responses shape, so a bridged ``/v1/responses`` turn told Codex the
cache was cold on every step of a thread the upstream had in fact reused.
Measured on a real 20-image replay: upstream ``prompt_cache_hit_tokens: 16000``
of 16224, client told ``cached_tokens: 0``.
"""

from __future__ import annotations

import unittest

from young_router.proxy import patches


class BridgeCacheUsageTests(unittest.TestCase):
    def test_a_provider_count_reaches_the_responses_shape(self) -> None:
        from litellm.responses.litellm_completion_transformation.transformation import (
            LiteLLMCompletionResponsesConfig,
        )
        from litellm.types.utils import Choices, ModelResponse, Usage

        patches._install_responses_bridge_cache_usage_patch()

        def transformed(**usage_fields) -> int:
            response = ModelResponse(
                id="chatcmpl-cache",
                created=1,
                model="deepseek-v4.1-flash",
                choices=[
                    Choices(
                        index=0,
                        message={"role": "assistant", "content": "ok"},
                        finish_reason="stop",
                    )
                ],
                usage=Usage(**usage_fields),
            )
            out = (
                LiteLLMCompletionResponsesConfig.transform_chat_completion_response_to_responses_api_response(
                    request_input=[],
                    chat_completion_response=response,
                    responses_api_request={},
                )
            )
            details = getattr(out.usage, "input_tokens_details", None)
            return int(getattr(details, "cached_tokens", 0) or 0)

        self.assertEqual(
            transformed(
                prompt_tokens=16_224,
                completion_tokens=8,
                total_tokens=16_232,
                prompt_cache_hit_tokens=16_000,
                prompt_cache_miss_tokens=224,
            ),
            16_000,
        )
        # An OpenAI-shaped usage is already correct and is left alone.
        self.assertEqual(
            transformed(
                prompt_tokens=100,
                completion_tokens=4,
                total_tokens=104,
                prompt_tokens_details={"cached_tokens": 90},
            ),
            90,
        )
        # No cache information at all stays zero rather than inventing one.
        self.assertEqual(
            transformed(prompt_tokens=100, completion_tokens=4, total_tokens=104),
            0,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
