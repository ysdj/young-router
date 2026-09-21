from __future__ import annotations

from hook_test_utils import *


class HookHostedComputerTests(HookTestCase):
    """The router runs no computer-use executor: a hosted ``computer`` tool is
    either served by the upstream itself or answered with an explicit refusal.

    Bridging such a request to chat would drop the hosted tool and answer as if
    computer use had never been requested, so that must never happen unless the
    client also brings its own computer or browser tools.
    """

    async def test_generic_response_wrapper_lets_native_hosted_computer_succeed(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "native": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="请打开浏览器看看",
            tools=[{"type": "computer"}],
            tool_choice="required",
            model_info={
                "id": "provider_alpha-generic-chat",
                "route_key": "provider_alpha / openai/vendor-chat / key=default",
            },
        )

        self.assertEqual(response, {"ok": True, "native": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tools"], [{"type": "computer"}])

    async def test_generic_response_wrapper_bridges_mcp_computer_use_namespace(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class ResponsesNotFound(Exception):
            status_code = 404

        error = ResponsesNotFound('OpenAIException - {"detail":"Not Found"}')

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise error
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="帮我看看 Safari 窗口",
            tools=[
                {"type": "computer"},
                {
                    "type": "namespace",
                    "name": "mcp__computer_use",
                    "description": "Control desktop apps.",
                    "tools": [
                        {
                            "type": "function",
                            "name": "get_app_state",
                            "description": "Get app state.",
                            "parameters": {
                                "type": "object",
                                "properties": {"app": {"type": "string"}},
                                "required": ["app"],
                            },
                        },
                        {
                            "type": "function",
                            "name": "click",
                            "description": "Click a point.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "app": {"type": "string"},
                                    "x": {"type": "number"},
                                    "y": {"type": "number"},
                                },
                                "required": ["app"],
                            },
                        },
                    ],
                },
            ],
            tool_choice="auto",
            model_info={
                "id": "provider_alpha-generic-chat",
                "route_key": "provider_alpha / openai/vendor-chat / key=default",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["use_chat_completions_api"])
        self.assertEqual(
            [tool["name"] for tool in calls[1]["tools"]],
            ["get_app_state", "click"],
        )

    async def test_generic_response_wrapper_reports_hosted_computer_unavailable(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class InvalidPrompt(Exception):
            status_code = 400

        error = InvalidPrompt(
            'OpenAIException - {"error":{"code":"invalid_prompt",'
            '"message":"Invalid Responses API request"},'
            '"metadata":{"raw":"invalid_union invalid_type expected string, received array"}}'
        )

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="请打开浏览器看看",
            tools=[{"type": "computer"}],
            tool_choice="required",
            model_info={
                "id": "provider_alpha-generic-chat",
                "route_key": "provider_alpha / openai/vendor-chat / key=default",
            },
        )

        # Only the native attempt ran: the refusal is reported, not bridged.
        self.assertEqual(len(calls), 1)
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["output"][0]["type"], "message")
        self.assertEqual(
            response["output"][0]["content"][0]["text"],
            hooks._HOSTED_COMPUTER_UNSUPPORTED_MESSAGE,
        )
        self.assertNotIn("computer_call", json.dumps(response))

    async def test_generic_response_wrapper_streams_hosted_computer_unavailable(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class ResponsesNotFound(Exception):
            status_code = 404

        error = ResponsesNotFound('OpenAIException - {"detail":"Not Found: computer tool unsupported"}')

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        stream = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="请打开浏览器看看",
            stream=True,
            tools=[{"type": "computer"}],
            model_info={
                "id": "provider_alpha-generic-chat",
                "route_key": "provider_alpha / openai/vendor-chat / key=default",
            },
        )
        raw_chunks = [chunk async for chunk in stream]
        chunks = [dict(chunk) for chunk in raw_chunks]

        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(str(raw_chunks[0]))["type"], "response.created")
        self.assertEqual(chunks[-1]["type"], "response.completed")
        deltas = [
            chunk.get("delta")
            for chunk in chunks
            if chunk.get("type") == "response.output_text.delta"
        ]
        self.assertEqual(deltas, [hooks._HOSTED_COMPUTER_UNSUPPORTED_MESSAGE])
        self.assertEqual(
            chunks[-1]["response"]["output"][0]["content"][0]["text"],
            hooks._HOSTED_COMPUTER_UNSUPPORTED_MESSAGE,
        )

    def test_chat_surface_never_gets_the_hosted_computer_message(self) -> None:
        """A chat request is never bridged, so its tool error stays its own.

        Chat Completions has no hosted computer tool at all; a ``computer``
        entry in chat ``tools`` is an invalid declaration that belongs to the
        upstream, not to the router.
        """

        hooks, _ = load_hook_module()

        class InvalidTool(Exception):
            status_code = 400

        error = InvalidTool("unsupported tool type: computer")
        chat_kwargs = {"call_type": "completion", "tools": [{"type": "computer"}]}

        self.assertIsNone(
            hooks._hosted_computer_unsupported_retry_response(error, chat_kwargs, None)
        )
        self.assertEqual(
            hooks._hosted_computer_unsupported_retry_response(
                error,
                {"call_type": "aresponses", "tools": [{"type": "computer"}]},
                None,
            )["output"][0]["content"][0]["text"],
            hooks._HOSTED_COMPUTER_UNSUPPORTED_MESSAGE,
        )

    def test_chat_bridge_is_blocked_for_a_hosted_computer_request_without_client_tools(self) -> None:
        hooks, _ = load_hook_module()

        self.assertTrue(
            hooks._request_hosted_computer_blocks_chat_bridge(
                {"tools": [{"type": "computer"}]},
            )
        )
        self.assertFalse(
            hooks._request_hosted_computer_blocks_chat_bridge(
                {"tools": [{"type": "function", "name": "exec_command"}]},
            )
        )
        self.assertFalse(
            hooks._request_hosted_computer_blocks_chat_bridge(
                {
                    "tools": [
                        {"type": "computer"},
                        {"type": "namespace", "name": "mcp__computer_use"},
                    ]
                },
            )
        )

    def test_no_computer_executor_surface_remains(self) -> None:
        hooks, _ = load_hook_module()

        for name in (
            "_COMPUTER_FACADE_BACKENDS",
            "_COMPUTER_FACADE_BACKEND_ENV",
            "_computer_facade_executor_registry",
            "_computer_facade_select_executor",
            "_run_computer_facade",
            "_responses_computer_facade_retry_response",
            "_COMPUTER_FACADE_SAFE_FAILURE_MESSAGE",
            "_COMPUTER_FACADE_MOCK_DONE_MESSAGE",
            "HostedToolPlan",
        ):
            if name == "HostedToolPlan":
                # The plan survives, but only as hosted-tool detection.
                self.assertFalse(
                    hasattr(
                        hooks.HostedToolPlan(hosted_computer=True),
                        "available_executor_hints",
                    )
                )
                continue
            self.assertFalse(hasattr(hooks, name), name)


if __name__ == "__main__":
    unittest.main()
