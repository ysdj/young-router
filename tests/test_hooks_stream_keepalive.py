from __future__ import annotations

import asyncio

from hook_test_utils import *


class StreamKeepaliveIntervalTests(HookTestCase):
    def test_default_interval_applies_without_configuration(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._STREAM_KEEPALIVE_INTERVAL_SECONDS_ENV, None)
        self.assertEqual(
            hooks._stream_keepalive_interval_seconds_for_request({"stream": True}),
            hooks._STREAM_KEEPALIVE_DEFAULT_INTERVAL_SECONDS,
        )

    def test_env_interval_and_disable_are_honored(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._STREAM_KEEPALIVE_INTERVAL_SECONDS_ENV, "7.5")
        self.assertEqual(
            hooks._stream_keepalive_interval_seconds_for_request({"stream": True}),
            7.5,
        )
        self.set_env(hooks._STREAM_KEEPALIVE_INTERVAL_SECONDS_ENV, "0")
        self.assertEqual(
            hooks._stream_keepalive_interval_seconds_for_request({"stream": True}),
            0.0,
        )

    def test_invalid_env_falls_back_to_default(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._STREAM_KEEPALIVE_INTERVAL_SECONDS_ENV, "not-a-number")
        self.assertEqual(
            hooks._stream_keepalive_interval_seconds_for_request({"stream": True}),
            hooks._STREAM_KEEPALIVE_DEFAULT_INTERVAL_SECONDS,
        )

    def test_request_metadata_overrides_env(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._STREAM_KEEPALIVE_INTERVAL_SECONDS_ENV, "30")
        request = {
            "stream": True,
            "metadata": {"stream_keepalive_interval_seconds": 0.05},
        }
        self.assertEqual(
            hooks._stream_keepalive_interval_seconds_for_request(request),
            0.05,
        )


class StreamKeepaliveChunkTests(HookTestCase):
    def test_keepalive_chunk_matches_each_wire_surface(self) -> None:
        hooks, _ = load_hook_module()
        responses_request = {"input": "hello", "stream": True}
        native_request = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
        other_request = {"model": "default-chat", "stream": True}

        responses_keepalive = hooks._stream_keepalive_chunk(responses_request, 3)
        self.assertIsInstance(responses_keepalive, dict)
        self.assertEqual(responses_keepalive["type"], "response.metadata")
        self.assertEqual(
            responses_keepalive["metadata"],
            {"phase": "keepalive", "sequence": 3},
        )
        self.assertTrue(hooks._is_stream_keepalive_chunk(responses_keepalive))

        native_keepalive = hooks._stream_keepalive_chunk(native_request, 4)
        self.assertIsInstance(native_keepalive, bytes)
        self.assertTrue(native_keepalive.startswith(b": litellm_menu keepalive "))
        self.assertTrue(hooks._is_stream_keepalive_chunk(native_keepalive))

        other_keepalive = hooks._stream_keepalive_chunk(other_request, 5)
        self.assertIsInstance(other_keepalive, str)
        self.assertTrue(other_keepalive.startswith(": litellm_menu keepalive "))
        self.assertTrue(hooks._is_stream_keepalive_chunk(other_keepalive))

    def test_keepalive_predicate_rejects_other_events(self) -> None:
        hooks, _ = load_hook_module()
        request = {"input": "hello", "stream": True}
        recovery_keepalive = hooks._route_recovery_sse_keepalive(
            1,
            request_data=request,
        )
        initial_keepalive = hooks._codex_responses_initial_sse_keepalive()
        real_chunk = {"type": "response.output_text.delta", "delta": "hi"}

        self.assertFalse(hooks._is_stream_keepalive_chunk(recovery_keepalive))
        self.assertFalse(hooks._is_stream_keepalive_chunk(initial_keepalive))
        self.assertFalse(hooks._is_stream_keepalive_chunk(real_chunk))
        self.assertFalse(hooks._is_stream_keepalive_chunk(None))


class DownstreamKeepaliveStreamTests(HookTestCase):
    async def test_silence_emits_keepalives_until_next_chunk(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "input": "hello",
            "stream": True,
            "metadata": {"stream_keepalive_interval_seconds": 0.05},
        }

        async def slow_stream():
            await asyncio.sleep(0.17)
            yield {"type": "response.output_text.delta", "delta": "ok"}

        chunks = [
            chunk
            async for chunk in hooks._yield_downstream_keepalive_stream(
                slow_stream(),
                request,
            )
        ]

        keepalives = [c for c in chunks if hooks._is_stream_keepalive_chunk(c)]
        real = [c for c in chunks if not hooks._is_stream_keepalive_chunk(c)]
        self.assertGreaterEqual(len(keepalives), 2)
        self.assertTrue(
            all(keepalives.index(c) < chunks.index(real[0]) for c in keepalives)
        )
        self.assertEqual(real, [{"type": "response.output_text.delta", "delta": "ok"}])

    async def test_active_delivery_does_not_emit_keepalives(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "input": "hello",
            "stream": True,
            "metadata": {"stream_keepalive_interval_seconds": 0.05},
        }

        async def steady_stream():
            for index in range(6):
                yield {"type": "response.output_text.delta", "delta": str(index)}
                await asyncio.sleep(0.02)

        chunks = [
            chunk
            async for chunk in hooks._yield_downstream_keepalive_stream(
                steady_stream(),
                request,
            )
        ]

        self.assertEqual(len(chunks), 6)
        self.assertFalse(any(hooks._is_stream_keepalive_chunk(c) for c in chunks))

    async def test_zero_interval_passes_chunks_through(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._STREAM_KEEPALIVE_INTERVAL_SECONDS_ENV, "0")
        request = {"input": "hello", "stream": True}

        async def slow_stream():
            await asyncio.sleep(0.12)
            yield {"type": "response.output_text.delta", "delta": "ok"}

        chunks = [
            chunk
            async for chunk in hooks._yield_downstream_keepalive_stream(
                slow_stream(),
                request,
            )
        ]

        self.assertEqual(chunks, [{"type": "response.output_text.delta", "delta": "ok"}])

    async def test_delivery_exception_propagates(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "input": "hello",
            "stream": True,
            "metadata": {"stream_keepalive_interval_seconds": 0.05},
        }

        async def failing_stream():
            yield {"type": "response.output_text.delta", "delta": "partial"}
            raise RuntimeError("upstream exploded")

        stream = hooks._yield_downstream_keepalive_stream(failing_stream(), request)
        first = await stream.__anext__()
        self.assertEqual(first["delta"], "partial")
        with self.assertRaises(RuntimeError):
            await stream.__anext__()

    async def test_early_close_cancels_pending_read(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "input": "hello",
            "stream": True,
            "metadata": {"stream_keepalive_interval_seconds": 0.05},
        }
        started = asyncio.Event()

        async def stalled_stream():
            yield {"type": "response.output_text.delta", "delta": "one"}
            started.set()
            await asyncio.sleep(30)
            yield {"type": "response.output_text.delta", "delta": "two"}

        stream = hooks._yield_downstream_keepalive_stream(stalled_stream(), request)
        first = await stream.__anext__()
        self.assertEqual(first["delta"], "one")
        keepalive = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
        self.assertTrue(hooks._is_stream_keepalive_chunk(keepalive))
        await stream.aclose()
        await asyncio.sleep(0)


class HookKeepaliveIntegrationTests(HookTestCase):
    async def test_hook_delivers_keepalive_during_buffered_start_silence(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._STREAM_KEEPALIVE_INTERVAL_SECONDS_ENV, "0.05")

        request_data = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Hello",
            "stream": True,
        }

        async def slow_upstream():
            await asyncio.sleep(0.14)
            yield {
                "type": "response.created",
                "response": {"id": "resp-keepalive-1"},
            }
            yield {
                "type": "response.output_text.delta",
                "delta": "hi",
            }
            yield {
                "type": "response.completed",
                "response": {"id": "resp-keepalive-1", "status": "completed"},
            }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=slow_upstream(),
                request_data=request_data,
            )
        ]

        keepalive_indexes = [
            index
            for index, chunk in enumerate(chunks)
            if hooks._is_stream_keepalive_chunk(chunk)
        ]
        delta_indexes = [
            index
            for index, chunk in enumerate(chunks)
            if isinstance(chunk, dict)
            and chunk.get("type") == "response.output_text.delta"
        ]
        self.assertGreaterEqual(len(keepalive_indexes), 1)
        self.assertTrue(delta_indexes, "expected the real delta to be delivered")
        self.assertLess(
            keepalive_indexes[0],
            delta_indexes[0],
            "keepalive must precede the first delivered output",
        )
