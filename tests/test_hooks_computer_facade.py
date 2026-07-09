from __future__ import annotations

from hook_test_utils import *


class HookComputerFacadeTests(HookTestCase):
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

    async def test_generic_response_wrapper_falls_back_to_facade_after_native_error(self) -> None:
        self.set_env("LITELLM_MENU_COMPUTER_FACADE_BACKEND", "mock")
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

        self.assertEqual(len(calls), 1)
        self.assertEqual(response["output"][0]["type"], "computer_call")
        self.assertEqual(response["output"][0]["actions"], [{"type": "screenshot"}])

    async def test_generic_response_wrapper_fallback_ignores_support_metadata_after_error(self) -> None:
        self.set_env("LITELLM_MENU_COMPUTER_FACADE_BACKEND", "mock")
        hooks, _ = load_hook_module()

        class InvalidTool(Exception):
            status_code = 400

        async def original_generic_function(**kwargs):
            raise InvalidTool("unsupported tool type: computer")

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="请打开浏览器看看",
            tools=[{"type": "computer"}],
            model_info={
                "id": "provider_alpha-generic-chat",
                "supports_computer_use": True,
            },
        )

        self.assertEqual(response["output"][0]["type"], "computer_call")

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
        dumped_retry = json.dumps(calls[1])
        self.assertNotIn("hosted_tool_unsupported", dumped_retry)

    async def test_generic_response_wrapper_streams_hosted_computer_use_unsupported(self) -> None:
        self.set_env("LITELLM_MENU_COMPUTER_FACADE_BACKEND", "mock")
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
        self.assertTrue(all(isinstance(chunk, dict) for chunk in raw_chunks))
        self.assertEqual(json.loads(str(raw_chunks[0]))["type"], "response.created")
        self.assertEqual(chunks[0]["type"], "response.created")
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertIn(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": chunks[1]["item"],
            },
            chunks,
        )
        self.assertEqual(chunks[1]["item"]["type"], "computer_call")
        self.assertEqual(chunks[2]["item"]["actions"], [{"type": "screenshot"}])


if __name__ == "__main__":
    unittest.main()
