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

    def test_deepseek_forced_choice_keeps_thinking_at_rust_dispatch(self) -> None:
        self.run_router_probe('''
            import asyncio
            from unittest.mock import patch

            import litellm
            from litellm import Router
            from litellm.proxy import proxy_server
            from young_router.proxy import patches

            patches.install_all()
            router = Router(model_list=[
                {
                    "model_name": "probe",
                    "litellm_params": {"model": "openai/fixture", "api_base": url, "order": order, "api_key": "sk-fake"},
                    "model_info": {"id": route},
                }
                for order, (route, url) in enumerate([
                    ("primary", "https://api.deepseek.com/v1"),
                    ("peer", "https://peer.example/v1"),
                ])
            ], num_retries=2, disable_cooldowns=True)
            proxy_server.llm_router = router

            async def main():
                calls = []

                async def provider(**kwargs):
                    route = kwargs["model_info"]["id"]
                    calls.append((route, kwargs.get("tool_choice"), kwargs.get("reasoning")))
                    return {"output": [], "status": "completed"}

                tools = [{"type": "function", "name": "fixture_tool", "parameters": {"type": "object"}}]
                async def request(choice):
                    return await router.aresponses(
                        model="probe", input="test", tools=tools, tool_choice=choice,
                        reasoning={"effort": "low"},
                    )

                with patch.object(litellm, "aresponses", provider):
                    named = {"type": "function", "name": "fixture_tool"}
                    await request("required")
                    await request(named)
                    await request("auto")
                    assert calls == [
                        ("primary", "auto", {"effort": "low"}),
                        ("primary", "auto", {"effort": "low"}),
                        ("primary", "auto", {"effort": "low"}),
                    ], calls

            asyncio.run(main())
            ''')

    def test_deepseek_refusal_repairs_unknown_route_at_rust_dispatch(self) -> None:
        self.run_router_probe('''
            import asyncio
            from types import SimpleNamespace
            from unittest.mock import patch

            import litellm
            from litellm import Router
            from litellm.proxy import proxy_server
            from young_router.proxy import patches

            patches.install_all()
            router = Router(model_list=[{
                "model_name": "probe",
                "litellm_params": {"model": "openai/fixture", "api_key": "sk-fake"},
                "model_info": {"id": "primary"},
            }], num_retries=0, disable_cooldowns=True)
            proxy_server.llm_router = router

            class Refusal(Exception):
                status_code = 400
                request = SimpleNamespace(
                    method="POST", url="https://api.openai.com/v1"
                )

            class TransportRefusal(Exception):
                request = SimpleNamespace(
                    method="POST", url="https://api.deepseek.com/v1/responses"
                )

            async def main():
                calls = []

                async def provider(**kwargs):
                    calls.append((kwargs["model_info"]["id"], kwargs.get("tool_choice"), kwargs.get("reasoning")))
                    if len(calls) == 1:
                        try:
                            raise TransportRefusal()
                        except TransportRefusal:
                            raise Refusal("thinking mode does not support this tool_choice") from None
                    return {"output": [], "status": "completed"}

                with patch.object(litellm, "aresponses", provider):
                    await router.aresponses(
                        model="probe", input="test",
                        tools=[{"type": "function", "name": "fixture_tool", "parameters": {"type": "object"}}],
                        tool_choice="required", reasoning={"effort": "high"},
                    )
                assert calls == [
                    ("primary", "required", {"effort": "high"}),
                    ("primary", "auto", {"effort": "high"}),
                ], calls

            asyncio.run(main())
            ''')

    def test_responses_wait_through_an_all_route_network_outage(self) -> None:
        self.run_router_probe('''
            import asyncio
            import os
            from unittest.mock import patch

            import litellm
            from litellm import Router
            from litellm.proxy import proxy_server
            from litellm.types.llms.openai import ResponseCompletedEvent, ResponsesAPIResponse
            from young_router.callbacks import image_generation_routing_hook
            from young_router.proxy import routing

            litellm.callbacks = [image_generation_routing_hook]
            router = Router(model_list=[{
                "model_name": "probe",
                "litellm_params": {"model": "openai/gpt-4o", "order": order, "api_key": "sk-fake"},
                "model_info": {"id": route},
            } for order, route in enumerate(("primary", "peer"))],
                num_retries=0, disable_cooldowns=True,
            )
            proxy_server.llm_router = router
            completed = ResponseCompletedEvent(type="response.completed", response=ResponsesAPIResponse(
                id="resp-probe", created_at=0, status="completed",
                output=[{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "back online"},
                ]}],
                usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            ))

            async def main():
                attempts = []
                online = False

                async def provider(**kwargs):
                    attempts.append(kwargs["model_info"]["id"])
                    if not online:
                        raise litellm.InternalServerError(
                            message="Cannot connect to host upstream.test:443",
                            model="gpt-4o", llm_provider="openai",
                        )

                    async def stream():
                        yield completed

                    return stream()

                async def request():
                    return await router.aresponses(model="probe", input="test", stream=True)

                with patch.object(litellm, "aresponses", provider), patch.dict(os.environ, {
                    "YOUNG_ROUTER_STREAM_HEDGE_ENABLED": "0",
                    "YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_FAILURES": "1",
                    "YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_SECONDS": "0.5",
                    "YOUNG_ROUTER_RECOVERY_MAX_SECONDS": "0.05",
                    "YOUNG_ROUTER_RECOVERY_INTERVAL_SECONDS": "0.02",
                }):
                    first = await request()
                    assert attempts == ["primary", "peer"], attempts
                    assert routing._is_route_recovery_stream_response(first), first
                    try:
                        # This request has never contacted an upstream. Its
                        # only evidence of the outage is the shared cooldown.
                        fresh = await request()
                        assert routing._is_route_recovery_stream_response(fresh), fresh
                        try:
                            opening = await anext(fresh)
                            assert opening["metadata"]["phase"] == "network", opening
                            async for frame in fresh:
                                if isinstance(frame, dict) and frame.get("metadata", {}).get("phase") == "cooldown":
                                    assert attempts == ["primary", "peer"], attempts
                                    online = True
                                if getattr(frame, "type", None) == "response.completed" or (
                                    isinstance(frame, dict) and frame.get("type") == "response.completed"
                                ):
                                    break
                            else:
                                raise AssertionError("the request did not complete after the upstream recovered")
                            assert online and len(attempts) == 3, attempts
                        finally:
                            await fresh.aclose()
                    finally:
                        await first.aclose()

            asyncio.run(asyncio.wait_for(main(), 5))
            ''')

    def test_a_real_rate_limit_is_not_an_unbounded_network_wait(self) -> None:
        self.run_router_probe('''
            import asyncio
            import os
            from unittest.mock import patch

            import litellm
            from litellm import Router
            from litellm.proxy import proxy_server
            from litellm.types.router import RouterRateLimitError
            from young_router.callbacks import image_generation_routing_hook

            litellm.callbacks = [image_generation_routing_hook]
            router = Router(model_list=[{
                "model_name": "probe", "model_info": {"id": "only"},
                "litellm_params": {"model": "openai/gpt-4o", "api_key": "sk-fake"},
            }], num_retries=0, disable_cooldowns=True)
            proxy_server.llm_router = router

            async def main():
                attempts = []
                async def refusal(**kwargs):
                    attempts.append(kwargs["model_info"]["id"])
                    raise litellm.RateLimitError(message="upstream quota exceeded", model="probe", llm_provider="openai")

                with patch.object(litellm, "aresponses", refusal), patch.dict(os.environ, {
                    "YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_FAILURES": "1", "YOUNG_ROUTER_STREAM_HEDGE_ENABLED": "0",
                }):
                    for _ in range(2):
                        try:
                            await router.aresponses(model="probe", input="test", stream=True)
                        except (litellm.RateLimitError, RouterRateLimitError) as exc:
                            assert exc.status_code == 429
                        else:
                            raise AssertionError("quota refusal opened a network recovery stream")
                    assert attempts == ["only"], attempts

            asyncio.run(main())
            ''')

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

    def test_an_order_emptied_by_the_filter_walks_to_the_next_one(self) -> None:
        """A filter that empties the walk's target order must not end the call.

        The head hedge reserves the deployment it is prefilling, and the
        deployment filter is where the walk learns that route is unavailable:
        the walk asks for its target order by name, so an empty answer has to
        mean "next order", not "no route at all".  Live 2026-11-26: the
        order-1 fallback landed on the deployment the hedge had
        been prefilling for ten seconds, and the relay refused both copies of
        the same 50 KB body; this pin is what makes the reservation actually
        change the route instead of ending the turn.
        """

        self.run_router_probe('''
            import asyncio
            from unittest.mock import patch

            import litellm
            from litellm import Router
            from litellm.integrations.custom_logger import CustomLogger
            from litellm.proxy import proxy_server
            from young_router.proxy import patches

            patches.install_all()

            class DropOrderOne(CustomLogger):
                """What the young-router filter does with a live hedge's route."""

                async def async_filter_deployments(
                    self, model, healthy_deployments, messages=None,
                    request_kwargs=None, parent_otel_span=None,
                ):
                    from young_router.proxy import responses_request

                    target_order = (request_kwargs or {}).get("_target_order")
                    if target_order is not None:
                        healthy_deployments = [
                            deployment for deployment in healthy_deployments
                            if responses_request._deployment_order(deployment)
                            == target_order
                        ]
                    return [
                        deployment for deployment in healthy_deployments
                        if responses_request._deployment_id(deployment) != "order1"
                    ]

            litellm.callbacks = [DropOrderOne()]
            router = Router(model_list=[
                {
                    "model_name": "probe",
                    "litellm_params": {"model": "openai/fixture", "api_key": "sk-fake", "order": order},
                    "model_info": {"id": route},
                }
                for order, route in enumerate(["order0", "order1", "order2"])
            ], num_retries=0, disable_cooldowns=True)
            proxy_server.llm_router = router

            class CapacityRefusal(Exception):
                status_code = 503

            async def main():
                calls = []

                async def provider(**kwargs):
                    route = (kwargs.get("model_info") or {}).get("id")
                    calls.append(route)
                    if route != "order2":
                        raise CapacityRefusal("all available accounts exhausted")
                    return {
                        "id": "answer",
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }],
                    }

                with patch.object(litellm, "acompletion", provider):
                    answer = await asyncio.wait_for(
                        router.acompletion(
                            model="probe",
                            messages=[{"role": "user", "content": "hi"}],
                        ),
                        20,
                    )
                assert calls == ["order0", "order2"], calls
                assert answer["_hidden_params"]["model_id"] == "order2", answer

            with patch("young_router.proxy.trace._route_trace"):
                asyncio.run(main())
            ''')
