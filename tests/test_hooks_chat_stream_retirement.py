from __future__ import annotations

from hook_test_utils import *

import asyncio


class ChatStreamRetirementTests(HookTestCase):
    """A replaced worker ends its own Chat turn instead of losing the socket.

    Observed live 2026-11-17 in Cherry Studio: a turn was streaming on a worker
    that a rolling reload retired.  The worker kept streaming while LiteLLM
    drained, then the process exit cut the SSE body mid-frame.  Cherry Studio's
    reader reports that half-written body as ``Invalid response: the provider
    response could not be processed`` -- the user is told the model answered
    garbage when the answer was fine and the transport was closed underneath it.

    The stop the shutdown patch now uses is uvicorn's own: the turn is cancelled
    once the drain gives up, and the wire must state the restart in the frame
    that closes it.  A cancellation is a ``BaseException``, so the ``except
    Exception`` every streaming path uses never sees it.
    """

    def chat_request(self) -> dict:
        return {
            "model": "default-chat",
            "stream": True,
            "proxy_server_request": {"path": "/v1/chat/completions"},
        }

    def chunk(self, text: str) -> dict:
        return {
            "id": "chatcmpl-retirement",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": text}}],
        }

    def terminal_chunk(self) -> dict:
        return {
            "id": "chatcmpl-retirement",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }

    def restart_stop(self, chunk: dict) -> dict:
        return ((chunk or {}).get("error") or {}) if isinstance(chunk, dict) else {}

    async def test_a_retiring_worker_closes_a_silent_turn_with_the_restart_stop(
        self,
    ) -> None:
        """The cancellation the shutdown patch sends must reach the client.

        The turn is suspended on an upstream that has not spoken yet, so there
        is no frame of its own to end it with: without the synthesized stop the
        client reads the closed body as an answer that was never finished.
        """
        hooks, _proxy_server = load_hook_module()

        release = asyncio.Event()

        async def upstream_stream():
            yield self.chunk("partial answer")
            await release.wait()
            yield self.chunk("never")

        chunks: list[dict] = []

        async def collect() -> None:
            async for chunk in hooks._yield_guarded_original_stream(
                [],
                upstream_stream(),
                self.chat_request(),
            ):
                chunks.append(jsonable_stream_chunk(chunk))

        hooks._begin_proxy_shutdown()
        try:
            task = asyncio.create_task(collect())
            await asyncio.sleep(0.1)
            task.cancel()
            await asyncio.wait_for(task, timeout=2.0)
        finally:
            hooks._end_proxy_shutdown()
            release.set()

        self.assertEqual(chunks[0].get("choices")[0]["delta"]["content"], "partial answer")
        stop = self.restart_stop(chunks[-1])
        self.assertEqual(stop.get("code"), "router_restarting")
        self.assertIn("restarting", stop.get("message", ""))
        self.assertFalse(
            any(
                "never" in json.dumps(chunk, ensure_ascii=False)
                for chunk in chunks
            ),
            "the frame the upstream had not sent yet must not be invented",
        )

    async def test_a_cancellation_that_is_not_this_workers_own_stays_cooperative(
        self,
    ) -> None:
        """A runtime cancellation is not this router's stop to report.

        Cancelling the task for its own reasons must keep propagating: turning
        it into an SSE error frame would swallow a signal the runtime is using
        to take the task down.
        """
        hooks, _proxy_server = load_hook_module()

        async def upstream_stream():
            yield self.chunk("partial answer")
            await asyncio.Event().wait()
            yield self.chunk("never")

        async def collect() -> None:
            async for _chunk in hooks._yield_guarded_original_stream(
                [],
                upstream_stream(),
                self.chat_request(),
            ):
                pass

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)

    async def test_a_chat_stream_cut_by_a_retirement_states_the_restart(self) -> None:
        """A body that closes with no terminal frame is not a finished answer.

        The upstream ended the body after some of the answer; the client reads
        that close as the turn's own end, so the restart has to be stated or the
        user keeps a half-written answer no one will ever finish.
        """
        hooks, _proxy_server = load_hook_module()

        async def upstream_stream():
            yield self.chunk("partial answer")

        hooks._begin_proxy_shutdown()
        try:
            chunks = [
                jsonable_stream_chunk(chunk)
                async for chunk in hooks._yield_guarded_original_stream(
                    [],
                    upstream_stream(),
                    self.chat_request(),
                )
            ]
        finally:
            hooks._end_proxy_shutdown()

        stop = self.restart_stop(chunks[-1])
        self.assertEqual(stop.get("code"), "router_restarting")

    async def test_a_healthy_stream_keeps_its_own_terminal(self) -> None:
        """Outside a retirement the route's own frames are delivered untouched."""
        hooks, _proxy_server = load_hook_module()

        async def upstream_stream():
            yield self.chunk("tok0 ")
            yield self.chunk("tok1 ")
            yield self.terminal_chunk()

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._yield_guarded_original_stream(
                [],
                upstream_stream(),
                self.chat_request(),
            )
        ]

        delivered = [
            chunk.get("choices")[0]["delta"].get("content")
            for chunk in chunks
            if chunk.get("choices")
            and chunk.get("choices")[0]["delta"].get("content")
        ]
        self.assertEqual(delivered, ["tok0 ", "tok1 "])
        self.assertEqual(
            chunks[-1].get("choices")[0].get("finish_reason"),
            "stop",
            "the healthy terminal is delivered as the upstream wrote it",
        )
        self.assertFalse(any(self.restart_stop(chunk) for chunk in chunks))

    async def test_the_wire_boundary_closes_a_cancelled_chat_turn(self) -> None:
        """Whatever layer the cancellation lands in, the body ends with a stop.

        The shutdown patch cancels the turn as a whole, so the cancellation can
        arrive between two frames instead of inside the upstream read.  The
        hook is the boundary every wire passes through, and it must state the
        restart rather than let the body close with no terminal.
        """
        hooks, _proxy_server = load_hook_module()
        hook = hooks.YoungRouterHook()

        async def upstream_stream():
            yield self.chunk("partial answer")
            await asyncio.Event().wait()

        chunks: list[dict] = []

        async def collect() -> None:
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=self.chat_request(),
            ):
                chunks.append(jsonable_stream_chunk(chunk))

        hooks._begin_proxy_shutdown()
        try:
            task = asyncio.create_task(collect())
            await asyncio.sleep(0.1)
            task.cancel()
            await asyncio.wait_for(task, timeout=2.0)
        finally:
            hooks._end_proxy_shutdown()

        self.assertTrue(
            any(
                chunk.get("choices")
                and chunk["choices"][0].get("delta", {}).get("content")
                for chunk in chunks
            ),
            "the answer that arrived is still delivered",
        )
        stop = self.restart_stop(chunks[-1])
        self.assertEqual(stop.get("code"), "router_restarting")

    async def test_the_restart_stop_is_json_on_the_chat_wire(self) -> None:
        """LiteLLM writes a Chat event as ``data: {chunk}`` -- str, not JSON.

        A plain dict therefore reaches the client as its Python repr, which no
        Chat client can parse (Cherry Studio reports it as ``Invalid JSON
        data``, i.e. one broken frame instead of the restart it was told
        about).  The synthesized event must stringify as JSON, the way the
        Responses events in this repo already do.
        """
        hooks, _proxy_server = load_hook_module()

        exception = RuntimeError("peer closed while the worker was replaced")
        hooks._mark_exception_as_local_shutdown(exception)
        chunk = hooks._synthesized_native_stream_error_chunk(
            self.chat_request(), exception
        )

        payload = json.loads(str(chunk))
        self.assertEqual(payload["error"]["code"], "router_restarting")

    async def test_a_chat_stream_that_ends_off_the_shutdown_path_still_fails(self) -> None:
        """Outside a retirement the missing terminal keeps its own meaning.

        The restart sentence is only honest while this worker is the one being
        replaced; an upstream that ends a turn without a terminal is reported as
        the incomplete stream it is.
        """
        hooks, _proxy_server = load_hook_module()

        async def upstream_stream():
            yield self.chunk("partial answer")

        with self.assertRaises(Exception) as raised:
            async for _chunk in hooks._yield_guarded_original_stream(
                [],
                upstream_stream(),
                self.chat_request(),
            ):
                pass

        self.assertIn("terminal", str(raised.exception).lower())


class RetireRemainingStreamsTests(HookTestCase):
    """The shutdown patch must cancel what the drain could not finish."""

    async def test_pending_tasks_are_cancelled_and_settled(self) -> None:
        hooks, _proxy_server = load_hook_module()
        from young_router.proxy import base

        stopped = asyncio.Event()

        async def silent() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        task = asyncio.create_task(silent())
        await asyncio.sleep(0.01)
        server = types.SimpleNamespace(
            server_state=types.SimpleNamespace(tasks={task}),
        )

        await asyncio.wait_for(base._retire_remaining_streams(server), timeout=3.0)

        self.assertTrue(task.cancelled() or task.done())
        self.assertTrue(stopped.is_set(), "the task was given its flush window")

    async def test_an_idle_worker_has_nothing_to_cancel(self) -> None:
        hooks, _proxy_server = load_hook_module()
        from young_router.proxy import base

        server = types.SimpleNamespace(
            server_state=types.SimpleNamespace(tasks=set()),
        )

        await asyncio.wait_for(base._retire_remaining_streams(server), timeout=1.0)


if __name__ == "__main__":
    unittest.main()
