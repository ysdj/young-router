from __future__ import annotations

import json

from hook_test_utils import *


class _ChunkStream:
    """The async byte iterator LiteLLM's Messages transport returns."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


def _sse(events) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode("utf-8")


def _a_full_turn() -> list[dict]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-fable-5-2",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "cache_read_input_tokens": 3},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "consider "},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig-1"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "Hel"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "lo"},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "shell",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '"ls"}'},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 34},
        },
        {"type": "message_stop"},
    ]


class AnthropicNonStreamBridgeTests(HookTestCase):
    async def test_a_whole_turn_is_rebuilt_from_its_frames(self) -> None:
        """The assembled message is what the non-streaming call would have returned."""

        bridge = _bridge_module()
        message = await bridge.assemble_anthropic_message(
            _ChunkStream([_sse(_a_full_turn())])
        )

        self.assertEqual("message", message["type"])
        self.assertEqual("msg_1", message["id"])
        self.assertEqual(3, len(message["content"]))
        thinking, text, tool_use = message["content"]
        self.assertEqual(
            {"type": "thinking", "thinking": "consider ", "signature": "sig-1"},
            thinking,
        )
        self.assertEqual({"type": "text", "text": "Hello"}, text)
        self.assertEqual("tool_use", tool_use["type"])
        self.assertEqual({"cmd": "ls"}, tool_use["input"])
        self.assertEqual("tool_use", message["stop_reason"])
        self.assertEqual(
            {"input_tokens": 12, "cache_read_input_tokens": 3, "output_tokens": 34},
            message["usage"],
        )

    async def test_a_frame_split_across_reads_is_still_one_event(self) -> None:
        """A socket read may end mid-frame; the tail waits for its boundary."""

        bridge = _bridge_module()
        wire = _sse(_a_full_turn())
        chunks = [wire[index : index + 7] for index in range(0, len(wire), 7)]

        message = await bridge.assemble_anthropic_message(_ChunkStream(chunks))

        self.assertEqual("Hello", message["content"][1]["text"])
        self.assertEqual({"cmd": "ls"}, message["content"][2]["input"])

    async def test_a_whole_body_turn_asks_the_route_for_a_stream(self) -> None:
        """The client asked for one message; the route is asked with frames."""

        bridge = _bridge_module()
        calls = []

        async def original(**kwargs):
            calls.append(kwargs)
            if kwargs.get("stream") is True:
                return _ChunkStream([_sse(_a_full_turn())])
            return {"whole": True}

        wrapped = bridge.wrap_deployment_function(
            original,
            method_name="anthropic_messages",
            client_streaming=False,
        )
        message = await wrapped(
            model="claude-fable-5-2",
            custom_llm_provider="anthropic",
            stream=False,
            messages=[{"role": "user", "content": "hi"}],
        )

        self.assertEqual(1, len(calls))
        self.assertIs(True, calls[0]["stream"])
        self.assertIs(
            True,
            calls[0]["litellm_metadata"][
                bridge._INNER_CALL_METADATA_KEY
            ],
        )
        self.assertEqual("message", message["type"])
        self.assertEqual("Hello", message["content"][1]["text"])

    async def test_a_custom_providers_anthropic_surface_is_still_bridged(self) -> None:
        """A route's provider name is not its wire.

        flux-code is a provider name this gateway registers; the deployment's
        wire is the Anthropic Messages surface.  Gating on the provider name
        skipped the bridge for exactly the routes the silent-connection
        ceiling breaks, so the surface the router selected is the gate.
        """

        bridge = _bridge_module()
        hooks, _ = load_hook_module()
        hooks._remember_selected_deployment(
            {
                "litellm_params": {"model": "anthropic/claude-fable-5-2"},
                "model_info": {"id": "flux-route"},
            },
            surface="anthropic",
        )
        calls = []

        async def original(**kwargs):
            calls.append(kwargs)
            return _ChunkStream([_sse(_a_full_turn())])

        wrapped = bridge.wrap_deployment_function(
            original,
            method_name="anthropic_messages",
            client_streaming=False,
        )
        message = await wrapped(
            stream=False,
            custom_llm_provider="flux-code",
            # The surface adapter writes this on every non-Responses wire; only
            # the Responses handler reads it.  It must not veto the bridge.
            use_chat_completions_api=True,
            model="flux-code/claude-fable-5-2",
            messages=[{"role": "user", "content": "hi"}],
        )

        self.assertEqual(1, len(calls))
        self.assertIs(True, calls[0]["stream"])
        self.assertEqual("message", message["type"])

    async def test_a_chat_surface_is_never_bridged_for_a_custom_provider(self) -> None:
        """The same provider name over the Chat surface answers OpenAI chunks."""

        bridge = _bridge_module()
        hooks, _ = load_hook_module()
        hooks._remember_selected_deployment(
            {
                "litellm_params": {"model": "openai/claude-fable-5-2"},
                "model_info": {"id": "flux-route"},
            },
            surface="openai/chat",
        )
        whole = {"id": "chatcmpl-1", "choices": []}

        async def original(**kwargs):
            return whole

        wrapped = bridge.wrap_deployment_function(
            original,
            method_name="anthropic_messages",
            client_streaming=False,
        )
        self.assertIs(
            whole,
            await wrapped(
                stream=False,
                custom_llm_provider="flux-code",
                model="flux-code/claude-fable-5-2",
            ),
        )

    async def test_the_inner_call_writes_no_second_request_row(self) -> None:
        """The bridged call is a transport detail, not a second turn.

        The proxy's callbacks log every streamed deployment call.  Without the
        marker the pane would show a streamed row and the client's own
        non-streaming row for one request -- and the mode column would report
        the wire the client never asked for.
        """

        bridge = _bridge_module()
        hooks, _ = load_hook_module()
        appended = []
        original_append = hooks._state_module._append_recent_request

        def record(entry, **kwargs):
            appended.append(entry)
            return "request-id"

        hooks._state_module._append_recent_request = record
        self.addCleanup(
            setattr,
            hooks._state_module,
            "_append_recent_request",
            original_append,
        )
        now = datetime.now(timezone.utc)
        hook = hooks.YoungRouterHook()
        inner = {
            "model": "claude-fable-5-2",
            "stream": True,
            "model_info": {"id": "relay-a"},
            "litellm_metadata": {bridge._INNER_CALL_METADATA_KEY: True},
        }

        await hook.async_log_success_event(inner, {"ok": True}, now, now)
        await hook.async_log_stream_event(inner, {"ok": True}, now, now)
        await hook.async_log_failure_event(inner, {"error": "x"}, now, now)
        self.assertEqual([], appended)

        # LiteLLM copies the inner call's metadata into the shared
        # ``litellm_params``; that copy must not make the client's own row
        # look like a second turn.
        await hook.async_log_success_event(
            {
                "model": "claude-fable-5-2",
                "stream": False,
                "model_info": {"id": "relay-a"},
                "litellm_params": {
                    "litellm_metadata": {
                        bridge._INNER_CALL_METADATA_KEY: True
                    }
                },
            },
            {"ok": True},
            now,
            now,
        )
        self.assertEqual(1, len(appended))
        self.assertIs(False, appended[0].get("stream"))
        appended.clear()

        # The client-facing answer still writes its row.
        await hook.async_log_success_event(
            {"model": "claude-fable-5-2", "stream": False, "model_info": {"id": "relay-a"}},
            {"ok": True},
            now,
            now,
        )
        self.assertEqual(1, len(appended))
        self.assertIs(False, appended[0].get("stream"))

    async def test_the_selected_deployments_marker_supplies_the_provider(self) -> None:
        """The deployment call may not carry ``custom_llm_provider`` at all."""

        bridge = _bridge_module()
        hooks, _ = load_hook_module()
        hooks._remember_selected_deployment(
            {
                "litellm_params": {"model": "anthropic/claude-fable-5-2"},
                "model_info": {"id": "relay-a"},
            }
        )
        calls = []

        async def original(**kwargs):
            calls.append(kwargs)
            return _ChunkStream([_sse(_a_full_turn())])

        wrapped = bridge.wrap_deployment_function(
            original,
            method_name="anthropic_messages",
            client_streaming=False,
        )
        message = await wrapped(stream=False)

        self.assertEqual(1, len(calls))
        self.assertIs(True, calls[0]["stream"])
        self.assertEqual("Hello", message["content"][1]["text"])

    async def test_a_streaming_client_keeps_its_frames(self) -> None:
        """Assembling a streamed client's answer would buffer its whole event stream."""

        bridge = _bridge_module()
        stream = _ChunkStream([b"event: ping\ndata: {\"type\":\"ping\"}\n\n"])

        async def original(**_kwargs):
            return stream

        wrapped = bridge.wrap_deployment_function(
            original,
            method_name="anthropic_messages",
            client_streaming=True,
        )
        self.assertIs(
            stream,
            await wrapped(stream=True, custom_llm_provider="anthropic"),
        )

    async def test_a_chat_bridge_route_is_never_assembled_here(self) -> None:
        """An OpenAI-chunk bridge answers no Anthropic frames."""

        bridge = _bridge_module()
        whole = {"id": "chatcmpl-1", "choices": []}

        async def original(**_kwargs):
            return whole

        wrapped = bridge.wrap_deployment_function(
            original,
            method_name="anthropic_messages",
            client_streaming=False,
        )
        self.assertIs(
            whole,
            await wrapped(
                stream=False,
                custom_llm_provider="openai",
                use_chat_completions_api=True,
            ),
        )

    async def test_an_error_frame_is_the_routes_failure(self) -> None:
        """A relay's in-stream refusal parks the route instead of ending the turn."""

        bridge = _bridge_module()
        events = _a_full_turn()[:1] + [
            {
                "type": "error",
                "error": {
                    "type": "upstream_error",
                    "message": "Upstream access forbidden, please contact administrator",
                },
            }
        ]

        with self.assertRaises(Exception) as context:
            await bridge.assemble_anthropic_message(_ChunkStream([_sse(events)]))

        self.assertEqual(502, getattr(context.exception, "status_code", None))
        self.assertIn("upstream access forbidden", str(context.exception).lower())

    async def test_a_stream_without_message_stop_is_not_a_shorter_answer(self) -> None:
        bridge = _bridge_module()
        events = _a_full_turn()[:-1]

        with self.assertRaises(Exception) as context:
            await bridge.assemble_anthropic_message(_ChunkStream([_sse(events)]))

        self.assertIs(True, getattr(context.exception, "stream_incomplete", False))

    async def test_a_tool_call_whose_input_never_parses_is_incomplete(self) -> None:
        bridge = _bridge_module()
        events = _a_full_turn()
        events[10] = {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'},
        }
        events[11] = {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '"ls"'},
        }

        with self.assertRaises(Exception) as context:
            await bridge.assemble_anthropic_message(_ChunkStream([_sse(events)]))

        self.assertIs(True, getattr(context.exception, "stream_incomplete", False))


def _bridge_module():
    load_hook_module()
    return sys.modules["young_router.proxy.anthropic_non_stream"]
