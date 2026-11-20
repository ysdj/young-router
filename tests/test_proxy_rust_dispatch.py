from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class RustDispatchTests(unittest.TestCase):
    def run_router_probe(self, code: str) -> None:
        # Hook tests install a fake LiteLLM in sys.modules. Exercise the
        # installed Router in a fresh process, mocking only the provider call.
        with tempfile.TemporaryDirectory() as state:
            result = subprocess.run(
                [os.environ.get("LITELLM_TEST_PYTHON") or sys.executable, "-c", textwrap.dedent(code)],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "LITELLM_RUNTIME_ROOT": state, "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_router_bounds_dispatch_and_races_with_separate_credentials(self) -> None:
        self.run_router_probe('''
            import asyncio
            import os
            from unittest.mock import patch
            import litellm
            from litellm import Router
            from litellm.proxy import proxy_server
            from young_router.proxy import patches, routing, trace

            patches.install_all()
            router = Router(model_list=[
                {
                    "model_name": "probe",
                    "litellm_params": {
                        "model": "openai/gpt-4o",
                        "order": order,
                        "api_key": f"sk-replace-{route}",
                    },
                    "model_info": {"id": route},
                }
                for order, route in enumerate(("slow", "fast"))
            ], num_retries=0)
            proxy_server.llm_router = router

            class Stream:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    return {"type": "response.output_text.delta", "delta": "sibling"}

                async def aclose(self):
                    pass

            async def main():
                attempts = []
                cancelled = []

                async def provider(**kwargs):
                    route = kwargs["model_info"]["id"]
                    attempts.append((route, kwargs["api_key"]))
                    if route == "fast":
                        return Stream()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        cancelled.append(route)
                        raise

                with patch.object(litellm, "aresponses", provider), patch.dict(os.environ, {
                    "YOUNG_ROUTER_STREAM_HEDGE_ENABLED": "1",
                    "YOUNG_ROUTER_STREAM_HEDGE_DELAY_SECONDS": "0.01",
                }):
                    response = await asyncio.wait_for(router.aresponses(
                        model="probe", input="test", stream=True, disable_fallbacks=True,
                    ), 3)
                    assert attempts == [("slow", "sk-replace-slow"), ("fast", "sk-replace-fast")], attempts
                    assert cancelled == ["slow"], cancelled
                    assert routing._selected_deployment_marker_from_response(response)["model_info"]["id"] == "fast"
                    assert (await anext(response))["delta"] == "sibling"
                    await response.aclose()

                for endpoint, payload in (
                    ("aresponses", {"input": "test"}),
                    ("acompletion", {"messages": [{"role": "user", "content": "test"}]}),
                    ("anthropic_messages", {"messages": [{"role": "user", "content": "test"}], "max_tokens": 100}),
                ):
                    with patch.object(litellm, endpoint, provider), patch.dict(os.environ, {
                        "YOUNG_ROUTER_STREAM_HEDGE_ENABLED": "0",
                        # The dispatch budget is the request's first-event
                        # budget, so a short first-event timeout is what makes
                        # these attempts expire.
                        "YOUNG_ROUTER_STREAM_START_TIMEOUT_SECONDS": "0.02",
                    }):
                        try:
                            async with asyncio.timeout(2):
                                result = await getattr(router, endpoint)(
                                    model="slow", stream=True, disable_fallbacks=True, **payload,
                                )
                        except TimeoutError as exc:
                            assert exc.body["reason"] == "stream_start_timeout", (endpoint, exc)
                            assert exc.body["start_seconds"] < 1, (endpoint, exc)
                            assert exc.failed_deployment_id == "slow", (endpoint, exc)
                        else:
                            raise AssertionError((endpoint, result))

            with patch.object(trace, "_route_trace"):
                asyncio.run(main())
            ''')

    def test_a_chat_head_is_raced_through_the_rust_driver(self) -> None:
        """The Rust driver bypasses ``Router.make_call``; the chat wire needs the
        head race too.

        A chat relay writes no response head until it has prefilled, exactly
        like the Responses replays the race was built for.  Measured live
        2026-11-20: a healthy gemini-3.8-flash route's first byte was 13.69 s
        away, while a hung sibling answered 524 after 126 s.
        """

        self.run_router_probe('''
            import asyncio
            import os
            from unittest.mock import patch
            import litellm
            from litellm import Router
            from litellm.proxy import proxy_server
            from young_router.proxy import patches, routing, trace

            patches.install_all()
            router = Router(model_list=[
                {
                    "model_name": "probe",
                    "litellm_params": {
                        "model": "openai/gpt-4o",
                        "order": order,
                        "api_key": f"sk-replace-{route}",
                    },
                    "model_info": {"id": route},
                }
                for order, route in enumerate(("slow", "fast"))
            ], num_retries=0)
            proxy_server.llm_router = router

            class Stream:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    return {"choices": [{"delta": {"content": "sibling"}}]}

                async def aclose(self):
                    pass

            async def main():
                attempts = []

                async def provider(**kwargs):
                    route = kwargs["model_info"]["id"]
                    attempts.append(route)
                    if route == "fast":
                        return Stream()
                    await asyncio.Future()

                with patch.object(litellm, "acompletion", provider), patch.dict(os.environ, {
                    "YOUNG_ROUTER_STREAM_HEDGE_ENABLED": "1",
                    "YOUNG_ROUTER_STREAM_HEDGE_DELAY_SECONDS": "0.01",
                }):
                    response = await asyncio.wait_for(router.acompletion(
                        model="probe",
                        messages=[{"role": "user", "content": "test"}],
                        stream=True,
                        disable_fallbacks=True,
                    ), 3)
                    assert attempts == ["slow", "fast"], attempts
                    marker = routing._selected_deployment_marker_from_response(response)
                    assert marker["model_info"]["id"] == "fast", marker
                    chunk = await anext(response)
                    assert chunk["choices"][0]["delta"]["content"] == "sibling", chunk
                    await response.aclose()

            with patch.object(trace, "_route_trace"):
                asyncio.run(main())
            ''')

    def test_native_responses_retry_retains_usage_and_closes_unread_stream(self) -> None:
        self.run_router_probe('''
            import asyncio
            from unittest.mock import AsyncMock, patch

            import litellm
            from litellm import Router
            from litellm.responses.streaming_iterator import (
                BaseResponsesAPIStreamingIterator, partial_responses_incomplete_event_on_disconnect,
            )
            from litellm.router_rust.core import RustCore
            from litellm.types.llms.openai import (
                ResponseCompletedEvent, ResponseIncompleteEvent, ResponsesAPIResponse,
            )
            from young_router.proxy import patches, trace

            patches.install_all()
            router = Router(model_list=[{
                "model_name": "probe",
                "litellm_params": {"model": "openai/gpt-4o", "api_key": "sk-fake"},
                "model_info": {"id": "route"},
            }], num_retries=0)
            completed = ResponseCompletedEvent(type="response.completed", response=ResponsesAPIResponse(
                id="resp-completed", created_at=0, status="completed",
                output=[{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "done"},
                ]}],
                usage={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            ))
            partial = ResponseIncompleteEvent(type="response.incomplete", response=ResponsesAPIResponse(
                id="resp-partial", created_at=0, status="incomplete", output=[],
            ))

            class NativeStream(BaseResponsesAPIStreamingIterator):
                def __init__(self, context_error=False):
                    self.context_error = context_error
                    self.completed_response = None
                    self.reads = 0
                    self.closed = False
                    self._hidden_params = {"additional_headers": {"x-probe": "present"}}

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    self.reads += 1
                    if self.context_error:
                        raise litellm.ContextWindowExceededError(
                            message="context_length_exceeded", model="gpt-4o", llm_provider="openai",
                        )
                    assert self.reads == 1, "the router must stop at the terminal event"
                    self.completed_response = completed
                    return completed

                async def aclose(self):
                    self.closed = True

                def partial_response_on_disconnect(self):
                    return partial

            async def main():
                attempts = []

                async def provider(**kwargs):
                    stream = NativeStream(context_error=not attempts)
                    attempts.append((kwargs.get("truncation"), stream))
                    return stream

                reporter = AsyncMock()
                with patch.object(litellm, "aresponses", provider), patch.object(
                    RustCore, "report_stream_success", reporter,
                ):
                    response = await router.aresponses(model="probe", input="test", stream=True)
                    assert isinstance(response, BaseResponsesAPIStreamingIterator)
                    assert response._hidden_params["additional_headers"]["x-probe"] == "present"
                    assert partial_responses_incomplete_event_on_disconnect(response) is partial
                    assert await anext(response) is completed
                    assert response.completed_response is completed
                    await response.aclose()
                    await asyncio.sleep(0)
                    assert [policy for policy, stream in attempts] == [None, "auto"], attempts
                    assert all(stream.closed for policy, stream in attempts)
                    reporter.assert_awaited_once()
                    assert reporter.await_args.args[0]["standard_logging_object"]["total_tokens"] == 7

                    unread = await router.aresponses(model="probe", input="test", stream=True)
                    await unread.aclose()
                    unread_source = attempts[-1][1]
                    assert unread_source.closed and unread_source.reads == 0
                    reporter.assert_awaited_once()

            with patch.object(trace, "_route_trace"):
                asyncio.run(main())
            ''')
