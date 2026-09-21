from __future__ import annotations

from hook_test_utils import *

import copy


CUA_REPL_NAMESPACE = {
    "type": "namespace",
    "name": "mcp__cua_repl",
    "description": "Computer use repl.",
    "tools": [
        {
            "type": "function",
            "name": "js",
            "description": "Run JavaScript in the computer-use repl.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
        {
            "type": "function",
            "name": "js_reset",
            "description": "Reset the repl.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "turn_ended",
            "description": "Notify the repl a turn ended.",
            "parameters": {"type": "object", "properties": {}},
        },
    ],
}


class HookComputerUseBridgeTests(HookTestCase):
    """Codex declares computer use as the ``mcp__cua_repl`` client namespace.

    When the selected route only speaks chat, the Responses request is bridged;
    those client tools must reach the model flattened, and their calls must come
    back carrying the namespace so Codex executes them on this machine.
    """

    def _codex_request(self) -> dict:
        return {
            "call_type": "aresponses",
            "model": "chat-only-probe",
            "input": [
                {"type": "additional_tools", "tools": [copy.deepcopy(CUA_REPL_NAMESPACE)]},
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "打开 Safari 并截图"}],
                },
            ],
            "stream": True,
        }

    def test_codex_computer_use_tools_survive_the_chat_bridge(self) -> None:
        hooks, _ = load_hook_module()
        retry_kwargs = self._codex_request()
        metadata = {"responses_chat_bridge": True}

        hooks._with_responses_chat_bridge_compatible_tools(retry_kwargs, metadata)

        bridged = {
            tool["name"]: tool.get(hooks._RESPONSES_BRIDGE_NAMESPACE_KEY)
            for tool in retry_kwargs.get("tools") or []
        }
        self.assertEqual(
            bridged,
            {
                "js": "mcp__cua_repl",
                "js_reset": "mcp__cua_repl",
                "turn_ended": "mcp__cua_repl",
            },
        )
        sanitized = metadata["responses_chat_bridge_tool_sanitized"]
        self.assertEqual(sanitized["bridged_namespace_tools"], 3)
        self.assertEqual(sanitized["dropped_types"], [])

        # The declaration moves from the Responses input item to the tools array.
        bridge_input, stats = hooks._responses_chat_bridge_input(retry_kwargs.get("input"))
        self.assertEqual(stats["dropped_additional_tools_items"], 1)
        self.assertEqual([item["role"] for item in bridge_input], ["user"])

    def _function_call(self, name: str) -> dict:
        return {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": name,
            "arguments": '{"code":"document.title"}',
            "status": "completed",
        }

    def test_bridged_computer_use_call_is_restored_with_its_namespace(self) -> None:
        hooks, _ = load_hook_module()
        request = self._codex_request()
        retry_kwargs = copy.deepcopy(request)
        metadata = {"responses_chat_bridge": True}
        hooks._with_responses_chat_bridge_compatible_tools(retry_kwargs, metadata)

        # Both the declaration site and the bridged tool array must resolve the
        # namespace, because the chat response only names the flat tool.
        mapping = hooks._responses_namespace_tool_map(request.get("input"), retry_kwargs)
        self.assertEqual(mapping.get("js"), "mcp__cua_repl")

        restored = hooks._restore_response_function_call_namespace(
            self._function_call("js"),
            mapping,
        )
        self.assertEqual(restored["namespace"], "mcp__cua_repl")
        self.assertEqual(restored["name"], "js")

        # A top-level function tool has no namespace and must never inherit one
        # from an unrelated namespace in the same request.
        unknown = hooks._restore_response_function_call_namespace(
            self._function_call("web_search"),
            mapping,
        )
        self.assertIsNone(unknown.get("namespace"))

    def test_streamed_computer_use_call_keeps_its_namespace(self) -> None:
        hooks, _ = load_hook_module()
        request = self._codex_request()
        retry_kwargs = copy.deepcopy(request)
        hooks._with_responses_chat_bridge_compatible_tools(
            retry_kwargs,
            {"responses_chat_bridge": True},
        )

        class Iterator:
            def __init__(self) -> None:
                self.request_input = request.get("input")
                self.responses_api_request = retry_kwargs
                self._pending_tool_events = [
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "js",
                            "arguments": "",
                            "status": "in_progress",
                        },
                    }
                ]

        iterator = Iterator()
        hooks._normalize_pending_tool_search_events(iterator)

        item = iterator._pending_tool_events[0]["item"]
        self.assertEqual(item["name"], "js")
        self.assertEqual(item["namespace"], "mcp__cua_repl")

    def test_a_client_that_brings_computer_use_keeps_the_chat_bridge(self) -> None:
        hooks, _ = load_hook_module()

        # Codex Desktop: hosted tool refused upstream, but the client also
        # brings its own executor, so the bridge must stay available.
        for namespace in ("mcp__cua_repl", "mcp__node_repl", "mcp__computer_use", "browser"):
            with self.subTest(namespace=namespace):
                self.assertFalse(
                    hooks._request_hosted_computer_blocks_chat_bridge(
                        {
                            "tools": [
                                {"type": "computer"},
                                {"type": "namespace", "name": namespace},
                            ]
                        }
                    )
                )
        # Without any client-side computer tool the hosted declaration must
        # never be dropped silently.
        self.assertTrue(
            hooks._request_hosted_computer_blocks_chat_bridge(
                {"tools": [{"type": "computer"}]}
            )
        )


    async def test_chat_only_route_answers_hosted_computer_use_explicitly(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        model_info = {
            "id": "provider_alpha-generic-chat",
            "route_key": "provider_alpha / openai/vendor-chat / key=default",
            "upstream_url_surface": "openai/chat",
        }

        # Chat-only route, no client-side computer use: the answer is explicit
        # instead of a chat request that cannot serve the hosted tool.
        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)
        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="请打开浏览器看看",
            tools=[{"type": "computer"}],
            tool_choice="required",
            model_info=dict(model_info),
        )
        self.assertEqual(calls, [])
        self.assertEqual(
            response["output"][0]["content"][0]["text"],
            hooks._HOSTED_COMPUTER_UNSUPPORTED_MESSAGE,
        )

        # The same client with its own computer use is bridged instead, so the
        # capability survives on this route.
        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)
        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input=self._codex_request()["input"],
            tools=[{"type": "computer"}],
            tool_choice="auto",
            model_info=dict(model_info),
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["use_chat_completions_api"])
        self.assertEqual(
            [tool["name"] for tool in calls[0]["tools"]],
            ["js", "js_reset", "turn_ended"],
        )



if __name__ == "__main__":
    unittest.main()
