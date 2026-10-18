from __future__ import annotations

import unittest

# Import the installed packages at module scope: the hook suites replace
# ``litellm`` and ``young_router.proxy`` in ``sys.modules`` while they run, and
# this test is about the real merge LiteLLM performs on its HTTP response.
import litellm
from litellm.proxy._types import UserAPIKeyAuth

try:
    from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing
    from litellm.proxy.utils import ProxyLogging
except Exception:  # pragma: no cover - exercised only without a bundled runtime
    ProxyBaseLLMRequestProcessing = None
    ProxyLogging = None

import young_router.callbacks as young_router_callbacks
from young_router.proxy import responses_execution as responses_execution_module


class _LoggingObject:
    litellm_call_id = "synthetic-call"
    litellm_params: dict = {}


class _FlaggedStream:
    """What the adapted Chat bridge hands to LiteLLM: a marked iterator."""

    _young_router_restored_reasoning = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _PlainStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@unittest.skipIf(ProxyLogging is None, "the real LiteLLM proxy is not installed")
class HookResponseHeaderTests(unittest.IsolatedAsyncioTestCase):
    """The restored-reasoning fact must reach the streaming HTTP header.

    Codex reads ``x-reasoning-included`` from the HTTP response before the first
    SSE event. A marker inside ``response.created`` never reaches that decision,
    which is why a 185k-token context kept looking like 400k and each turn
    started an unnecessary pre-turn compaction.
    """

    def setUp(self) -> None:
        self._previous_callbacks = litellm.callbacks
        litellm.callbacks = [young_router_callbacks.image_generation_routing_hook]
        self.addCleanup(setattr, litellm, "callbacks", self._previous_callbacks)

    async def _final_stream_headers(self, response: object) -> dict:
        callback_headers = await ProxyLogging(
            user_api_key_cache=None,
            premium_user=False,
        ).post_call_response_headers_hook(
            data={"model": "synthetic-route"},
            user_api_key_dict=UserAPIKeyAuth(),
            response=response,
            request_headers={},
        )
        return dict(
            ProxyBaseLLMRequestProcessing(
                {"model": "synthetic-route", "litellm_call_id": "synthetic-call"}
            )._stream_response_headers(
                hidden_params={},
                user_api_key_dict=UserAPIKeyAuth(),
                logging_obj=_LoggingObject(),
                version="0.0.0",
                callback_headers=callback_headers or {},
            )
        )

    def test_hook_is_registered_where_litellm_detects_it(self) -> None:
        hook_type = type(young_router_callbacks.image_generation_routing_hook)

        self.assertIn("async_post_call_response_headers_hook", hook_type.__dict__)
        self.assertTrue(
            ProxyLogging.callback_capabilities().has_post_call_response_headers
        )

    async def test_adapted_stream_sets_the_header_codex_reads(self) -> None:
        headers = await self._final_stream_headers(_FlaggedStream())

        self.assertEqual("true", headers.get("x-reasoning-included"))
        self.assertEqual(
            responses_execution_module.REASONING_INCLUDED_HEADER,
            "x-reasoning-included",
        )

    async def test_plain_stream_keeps_the_header_the_upstream_sent(self) -> None:
        headers = await self._final_stream_headers(_PlainStream())

        self.assertNotIn("x-reasoning-included", headers)
