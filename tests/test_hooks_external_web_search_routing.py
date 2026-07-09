from __future__ import annotations

import copy
import ssl

from hook_test_utils import *


class HookExternalWebSearchRoutingTests(HookTestCase):
    def test_external_web_search_action_text_parses_open_page_queries(self) -> None:
        hooks, _ = load_hook_module()

        self.assertEqual(
            hooks._litellm_web_search_action_from_call(
                {
                    "type": "function_call",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": "openPage: https://example.test/articles/fulltext",
                }
            ),
            {
                "type": "openPage",
                "url": "https://example.test/articles/fulltext",
            },
        )
        self.assertEqual(
            hooks._litellm_web_search_action_from_call(
                {
                    "type": "function_call",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": json.dumps(
                        {
                            "query": "openPage: https://example.test/articles/secondary",
                        }
                    ),
                }
            ),
            {
                "type": "openPage",
                "url": "https://example.test/articles/secondary",
            },
        )

    def test_external_web_search_structured_url_arguments_parse_read_and_find(self) -> None:
        hooks, _ = load_hook_module()

        self.assertEqual(
            hooks._litellm_web_search_action_from_call(
                {
                    "type": "function_call",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": json.dumps(
                        {"url": "https://example.test/article"}
                    ),
                }
            ),
            {"type": "openPage", "url": "https://example.test/article"},
        )
        self.assertEqual(
            hooks._litellm_web_search_action_from_call(
                {
                    "type": "function_call",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": json.dumps(
                        {"url": "https://example.test/article", "pattern": "factor A"}
                    ),
                }
            ),
            {
                "type": "findInPage",
                "url": "https://example.test/article",
                "pattern": "factor A",
            },
        )

    def test_external_web_search_ignores_truncated_tool_json_as_search_query(self) -> None:
        hooks, _ = load_hook_module()

        self.assertIsNone(
            hooks._litellm_web_search_action_from_call(
                {
                    "type": "function_call",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": json.dumps({"query": '{"url": "https://example.test'}),
                }
            )
        )

        item = hooks._external_web_search_call_item_for_action(
            {"type": "search", "query": '{"url": "https://example.test'},
        )
        self.assertIsNone(item)

    def test_external_web_search_visible_page_actions_render_as_search_rows(self) -> None:
        hooks, _ = load_hook_module()

        open_item = hooks._external_web_search_call_item_for_action(
            {"type": "openPage", "url": "https://example.test/fulltext"},
        )
        self.assertEqual(open_item["action"]["type"], "search")
        self.assertIn("https://example.test/fulltext", open_item["action"]["query"])

        find_item = hooks._external_web_search_call_item_for_action(
            {
                "type": "findInPage",
                "url": "https://example.test/fulltext",
                "pattern": "factor A",
            },
        )
        self.assertEqual(find_item["action"]["type"], "search")
        self.assertIn("https://example.test/fulltext", find_item["action"]["query"])
        self.assertIn("factor A", find_item["action"]["query"])

    def test_external_web_search_visible_items_do_not_expose_bridge_internals(self) -> None:
        hooks, _ = load_hook_module()

        open_item = hooks._external_web_search_call_item_for_action(
            {"type": "openPage", "url": "https://example.test/article"},
            ["https://example.test/article"],
        )
        find_item = hooks._sanitize_web_search_call_item(
            {
                "type": "web_search_call",
                "action": {
                    "type": "findInPage",
                    "url": "https://example.test/article",
                    "pattern": "factor A",
                },
            },
            ["https://example.test/article"],
        )

        self.assertIsNotNone(open_item)
        self.assertIsNotNone(find_item)
        dumped = json.dumps([open_item, find_item])
        self.assertNotIn("bridge_action", dumped)
        self.assertNotIn('"type": "openPage"', dumped)
        self.assertNotIn('"type": "findInPage"', dumped)
        self.assertEqual(open_item["action"]["type"], "search")
        self.assertEqual(find_item["action"]["type"], "search")
        self.assertIn("https://example.test/article", dumped)

    async def test_external_web_search_bad_response_status_retries_bridge_once(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class BadResponseStatus(Exception):
            status_code = 400

        error = BadResponseStatus(
            'OpenAIException - {"error":{"message":"openai_error",'
            '"type":"bad_response_status_code","param":"",'
            '"code":"bad_response_status_code"}}'
        )

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise error
            return {
                "id": "resp_retry_ok",
                "object": "response",
                "status": "completed",
                "output_text": "retry ok",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "retry ok"}],
                    }
                ],
            }

        bridge_kwargs = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": False,
            "tools": [
                {"type": "function", "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME}
            ],
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
            },
        }

        response = await hooks._execute_responses_external_web_search_bridge_call(
            original_generic_function,
            bridge_kwargs,
            original_request_kwargs=bridge_kwargs,
        )

        self.assertEqual(response.get("output_text"), "retry ok")
        self.assertEqual(len(calls), 2)

    async def test_external_web_search_bad_response_status_does_not_dump_fallback_after_bridge_retry(self) -> None:
        hooks, _ = load_hook_module()
        calls = []
        web_search_bridge_module = importlib.import_module(
            "litellm_menu.responses_web_search_bridge"
        )
        original_run_actions = web_search_bridge_module._external_web_search_run_actions
        run_actions_called = False

        class BadResponseStatus(Exception):
            status_code = 400

        error = BadResponseStatus(
            'OpenAIException - {"error":{"message":"openai_error",'
            '"type":"bad_response_status_code","param":"",'
            '"code":"bad_response_status_code"}}'
        )

        async def fake_run_actions(actions, page_cache, page_fetch_tasks):
            nonlocal run_actions_called
            run_actions_called = True
            return (
                "Web search results for query: Sample City weather\n"
                "Title: Weather Source\n"
                "URL: https://example.test/weather\n"
                "Snippet: Sample City is sunny.",
                ["https://example.test/weather"],
                [["https://example.test/weather"]],
                actions,
            )

        web_search_bridge_module._external_web_search_run_actions = fake_run_actions
        self.addCleanup(
            setattr,
            web_search_bridge_module,
            "_external_web_search_run_actions",
            original_run_actions,
        )

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            raise error

        bridge_kwargs = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for Sample City weather. Answer briefly with source URLs.",
            "stream": False,
            "tools": [
                {"type": "function", "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME}
            ],
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
            },
        }

        with self.assertRaises(BadResponseStatus):
            await hooks._execute_responses_external_web_search_bridge_call(
                original_generic_function,
                bridge_kwargs,
                original_request_kwargs=bridge_kwargs,
            )
        self.assertEqual(len(calls), 2)
        self.assertFalse(run_actions_called)

    async def test_external_web_search_stream_start_timeout_does_not_dump_direct_fallback(self) -> None:
        hooks, _ = load_hook_module()
        calls = []
        web_search_bridge_module = importlib.import_module(
            "litellm_menu.responses_web_search_bridge"
        )
        original_run_actions = web_search_bridge_module._external_web_search_run_actions
        run_actions_called = False

        async def fake_run_actions(actions, page_cache, page_fetch_tasks):
            nonlocal run_actions_called
            run_actions_called = True
            return (
                "Web search results for query: sample subject Signal\n"
                "Title: Signal Source\n"
                "URL: https://example.test/erk\n"
                "Snippet: Sample subject and Signal pathway evidence.",
                ["https://example.test/erk"],
                [["https://example.test/erk"]],
                actions,
            )

        web_search_bridge_module._external_web_search_run_actions = fake_run_actions
        self.addCleanup(
            setattr,
            web_search_bridge_module,
            "_external_web_search_run_actions",
            original_run_actions,
        )

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            exc = TimeoutError("stream start timeout")
            exc.status_code = 504
            exc.body = {"reason": "stream_start_timeout"}
            raise exc

        bridge_kwargs = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for sample subject Signal. Answer briefly with source URLs.",
            "stream": True,
            "tools": [
                {"type": "function", "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME}
            ],
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
            },
        }

        with self.assertRaises(TimeoutError):
            await hooks._execute_responses_external_web_search_bridge_call(
                original_generic_function,
                bridge_kwargs,
                original_request_kwargs=bridge_kwargs,
            )
        self.assertEqual(len(calls), 1)
        self.assertFalse(run_actions_called)

    async def test_external_web_search_sanitized_auth_balance_failure_is_not_direct_fallback(self) -> None:
        hooks, _ = load_hook_module()
        web_search_bridge_module = importlib.import_module(
            "litellm_menu.responses_web_search_bridge"
        )
        original_run_actions = web_search_bridge_module._external_web_search_run_actions
        run_actions_called = False

        class UpstreamBalanceError(Exception):
            status_code = 400

        upstream_error = UpstreamBalanceError('{"code":"INSUFFICIENT_BALANCE"}')
        sanitized_error = hooks._sanitized_upstream_route_exception(
            "balanced-chat",
            upstream_error,
            {"model": "balanced-chat"},
        )

        async def fake_run_actions(actions, page_cache, page_fetch_tasks):
            nonlocal run_actions_called
            run_actions_called = True
            return "", [], [], actions

        web_search_bridge_module._external_web_search_run_actions = fake_run_actions
        self.addCleanup(
            setattr,
            web_search_bridge_module,
            "_external_web_search_run_actions",
            original_run_actions,
        )

        async def original_generic_function(**kwargs):
            raise sanitized_error

        bridge_kwargs = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": False,
            "tools": [
                {"type": "function", "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME}
            ],
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
            },
        }

        with self.assertRaises(Exception) as context:
            await hooks._execute_responses_external_web_search_bridge_call(
                original_generic_function,
                bridge_kwargs,
                original_request_kwargs=bridge_kwargs,
            )

        self.assertIs(context.exception, sanitized_error)
        self.assertFalse(run_actions_called)

    async def test_external_web_search_bridge_preemptively_maps_xhigh_to_low(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def upstream_stream():
            yield {"type": "response.output_text.delta", "delta": "OK"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_preemptive",
                    "object": "response",
                    "status": "completed",
                    "output_text": "OK",
                    "output": [],
                },
            }

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return upstream_stream()

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="openai/vendor-chat",
            input=[{"role": "user", "content": "hi5.4"}],
            stream=True,
            reasoning={"effort": "xhigh"},
            text={"verbosity": "low"},
            tools=[
                {"type": "web_search"},
                {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
            ],
            tool_choice="auto",
            model_info={
                "id": "79f0dc70",
                "provider": "provider_chat",
                "route_key": "provider_chat / openai/vendor-chat / key=default",
                "upstream_url_surface": "openai/responses",
                "supports_responses_web_search": False,
            },
        )

        self.assertTrue(hooks._response_is_async_iterable(response))
        chunks = []
        async for chunk in response:
            chunks.append(chunk)
        self.assertEqual(chunks[0]["delta"], "OK")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["reasoning"]["effort"], "low")
        self.assertTrue(
            calls[0]["litellm_metadata"][hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY]
        )
        self.assertNotIn("use_chat_completions_api", calls[0])

    async def test_external_web_search_function_bridge_forces_low_reasoning(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def retry_stream():
            yield {"type": "response.output_text.delta", "delta": "OK"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_retry",
                    "object": "response",
                    "status": "completed",
                    "output_text": "OK",
                    "output": [],
                },
            }

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return retry_stream()

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input=[{"role": "user", "content": "hi5.4"}],
            stream=True,
            reasoning={"effort": "xhigh"},
            text={"verbosity": "low"},
            tools=[
                {"type": "web_search"},
                {
                    "type": "function",
                    "name": "exec_command",
                    "description": "Run a command.",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "required": ["cmd"],
                    },
                },
            ],
            tool_choice="auto",
            model_info={
                "id": "generic-responses",
                "provider": "generic",
                "route_key": "generic / openai/balanced-chat / key=default",
                "upstream_url_surface": "openai/responses",
                "supports_responses_web_search": False,
            },
        )

        self.assertTrue(hooks._response_is_async_iterable(response))
        chunks = []
        async for chunk in response:
            chunks.append(chunk)
        self.assertEqual(chunks[0]["delta"], "OK")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["reasoning"]["effort"], "low")
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertTrue(
            calls[0]["litellm_metadata"][hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY]
        )
        self.assertNotIn(
            hooks._XHIGH_REASONING_COMPAT_RETRY_METADATA_KEY,
            calls[0]["litellm_metadata"],
        )

    async def test_generic_response_wrapper_keeps_external_web_search_stream_iterable(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def upstream_stream():
            yield {"type": "response.output_text.delta", "delta": "STREAM_OK"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream",
                    "object": "response",
                    "status": "completed",
                    "output_text": "STREAM_OK",
                    "output": [],
                },
            }

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return upstream_stream()

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="No search needed.",
            stream=True,
            tools=[
                {"type": "web_search"},
                {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
            ],
            tool_choice="auto",
            model_info={
                "id": "provider_beta-generic-chat",
                "provider": "provider_beta",
                "route_key": "provider_beta / openai/vendor-chat / key=default",
                "upstream_url_surface": "openai/responses",
                "supports_responses_web_search": False,
            },
        )

        self.assertTrue(hooks._response_is_async_iterable(response))
        self.assertEqual(len(calls), 1)
        self.assertEqual([tool.get("type") for tool in calls[0]["tools"]], ["function", "function"])
        self.assertEqual(
            calls[0]["tools"][0]["name"],
            hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
        )

        chunks = []
        async for chunk in response:
            chunks.append(chunk)
        self.assertEqual(chunks[0]["delta"], "STREAM_OK")
        self.assertNotIn("hosted_tool_unsupported", json.dumps(chunks))

    async def test_external_web_search_stream_ignores_empty_bridge_call_without_replay(self) -> None:
        hooks, _ = load_hook_module()

        async def upstream_stream():
            yield {"type": "response.output_text.delta", "delta": "VISIBLE"}
            yield {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "call_empty_search",
                    "call_id": "call_empty_search",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": "{}",
                    "status": "completed",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream",
                    "object": "response",
                    "status": "completed",
                    "output_text": "VISIBLE",
                    "output": [
                        {
                            "id": "msg_visible",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "VISIBLE",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "balanced-chat",
            "input": "No search needed.",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "parameters": {"type": "object"},
                }
            ],
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
            },
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        visible_deltas = [
            chunk
            for chunk in chunks
            if isinstance(chunk, dict)
            and chunk.get("type") == "response.output_text.delta"
            and chunk.get("delta") == "VISIBLE"
        ]
        self.assertEqual(len(visible_deltas), 1)
        self.assertEqual(chunks[-1]["type"], "response.completed")

    def test_external_web_search_bridge_skips_native_web_search_route_when_explicitly_supported(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "Search the web.",
            "tools": [{"type": "web_search"}],
            "model_info": {
                "id": "openai-native",
                "provider": "openai",
                "supports_responses_web_search": True,
            },
        }

        self.assertIsNone(hooks._with_responses_external_web_search_bridge(request_kwargs))

    def test_external_web_search_bridge_tries_unknown_large_natively(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "Search the web.",
            "tools": [{"type": "web_search"}],
            "model_info": {
                "id": "generic-large",
                "provider": "generic",
            },
        }

        self.assertIsNone(hooks._with_responses_external_web_search_bridge(request_kwargs))

    def test_external_web_search_bridge_tries_selected_gpt_route_natively_when_unknown(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "Search the web.",
            "tools": [{"type": "web_search"}],
            "model_info": {
                "id": "compat_provider-large",
                "provider": "compat_provider",
                "route_key": "compat_provider / openai/default-chat / key=x-plus / order=2",
                "upstream_url_surface": "openai/responses",
            },
        }

        self.assertIsNone(hooks._with_responses_external_web_search_bridge(request_kwargs))

    def test_external_web_search_bridge_tries_selected_generic_chat_route_natively_when_unknown(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "legacy-chat",
            "input": "Search the web.",
            "tools": [{"type": "web_search"}],
            "model_info": {
                "id": "provider_alpha-chatroute",
                "provider": "provider_alpha",
                "route_key": "provider_alpha / openai/vendor-chat / key=default / order=1",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
            },
        }

        self.assertIsNone(hooks._with_responses_external_web_search_bridge(request_kwargs))

    def test_external_web_search_bridge_uses_explicit_false_support_metadata(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "Search the web.",
            "tools": [{"type": "web_search"}],
            "model_info": {
                "id": "generic-large-no-native-search",
                "provider": "generic",
                "supports_responses_web_search": False,
            },
        }

        bridge_kwargs = hooks._with_responses_external_web_search_bridge(request_kwargs)
        self.assertIsNotNone(bridge_kwargs)
        assert bridge_kwargs is not None
        self.assertTrue(bridge_kwargs["litellm_metadata"][hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY])

    async def test_external_web_search_unknown_native_support_falls_back_after_tool_error(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class ProviderBadRequest(Exception):
            status_code = 400

        unsupported_web_search_error = ProviderBadRequest(
            'OpenAIException - {"error":{"message":"Unsupported tool type: '
            'web_search","type":"invalid_request_error"}}'
        )

        async def upstream_stream():
            yield {"type": "response.output_text.delta", "delta": "OK"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_web_search_bridge_retry",
                    "object": "response",
                    "status": "completed",
                    "output_text": "OK",
                    "output": [],
                },
            }

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise unsupported_web_search_error
            return upstream_stream()

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="legacy-chat",
            input="Search the web.",
            stream=True,
            tools=[{"type": "web_search"}],
            model_info={
                "id": "provider_alpha-chatroute",
                "provider": "provider_alpha",
                "route_key": "provider_alpha / openai/vendor-chat / key=default / order=1",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
            },
        )

        self.assertTrue(hooks._response_is_async_iterable(response))
        chunks = []
        async for chunk in response:
            chunks.append(chunk)
        self.assertEqual(chunks[0]["delta"], "OK")
        self.assertEqual(len(calls), 2)
        self.assertEqual([tool.get("type") for tool in calls[0]["tools"]], ["web_search"])
        self.assertEqual([tool.get("type") for tool in calls[1]["tools"]], ["function"])
        self.assertEqual(calls[1]["tools"][0]["name"], hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME)
        self.assertTrue(calls[1]["litellm_metadata"][hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY])
        self.assertTrue(calls[1]["litellm_metadata"]["external_web_search_native_error_fallback"])

    async def test_external_web_search_unknown_native_support_does_not_fallback_on_policy_error(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class ProviderBadRequest(Exception):
            status_code = 400

        policy_error = ProviderBadRequest(
            'OpenAIException - {"error":{"message":"Request violates content '
            'policy while using web_search","type":"invalid_request_error"}}'
        )

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            raise policy_error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(ProviderBadRequest):
            await request_kwargs["original_generic_function"](
                call_type="aresponses",
                model="default-chat",
                input="Search the web.",
                tools=[{"type": "web_search"}],
                model_info={
                    "id": "generic-large",
                    "provider": "generic",
                    "upstream_url_surface": "openai/responses",
                },
            )

        self.assertEqual(len(calls), 1)

    def test_external_web_search_bridge_accepts_explicit_chatroute_native_support(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Search the web.",
            "tools": [{"type": "web_search"}],
            "model_info": {
                "id": "chatroute-native",
                "provider": "provider_chat",
                "supports_responses_web_search": True,
            },
        }

        self.assertIsNone(hooks._with_responses_external_web_search_bridge(request_kwargs))

    def test_external_web_search_synthesis_does_not_reenter_bridge_from_outer_tools(self) -> None:
        hooks, _ = load_hook_module()
        original_request = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "请使用 web_search 查询Sample City weather",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "custom_tools": [{"name": "apply_patch"}],
            "functions": [{"name": "exec_command"}],
            "mcp_servers": [{"name": "computer-use"}],
            "tool_resources": {"file_search": {"vector_store_ids": ["vs_1"]}},
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY: True,
            },
        }

        synthesis_kwargs = hooks._external_web_search_synthesis_kwargs(
            original_request,
            "Web search results for query: Sample City weather\nTitle: source\nURL: https://example.test\nSnippet: warm",
        )

        self.assertFalse(hooks._request_should_intercept_external_web_search(synthesis_kwargs))
        self.assertNotIn("tools", synthesis_kwargs)
        self.assertNotIn("custom_tools", synthesis_kwargs)
        self.assertNotIn("functions", synthesis_kwargs)
        self.assertNotIn("mcp_servers", synthesis_kwargs)
        self.assertNotIn("tool_resources", synthesis_kwargs)
        self.assertNotIn("use_chat_completions_api", synthesis_kwargs)
        self.assertNotIn(hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY, synthesis_kwargs["litellm_metadata"])
        self.assertNotIn(
            hooks._WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY,
            synthesis_kwargs["litellm_metadata"],
        )
        self.assertNotIn(
            hooks._RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY,
            synthesis_kwargs["litellm_metadata"],
        )
        self.assertTrue(
            synthesis_kwargs["litellm_metadata"][
                "external_web_search_synthesis"
            ]
        )
        self.assertTrue(
            synthesis_kwargs["litellm_metadata"][
                hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY
            ]
        )

        bridge_kwargs = hooks._responses_chat_bridge_preemptive_kwargs(
            synthesis_kwargs,
            original_request,
            include_hosted_web_search_unsupported=True,
            include_client_tool_unsupported=True,
        )

        self.assertIsNone(bridge_kwargs)

        synthesis_metadata_kwargs = synthesis_kwargs.copy()
        synthesis_metadata_kwargs["metadata"] = synthesis_metadata_kwargs.pop(
            "litellm_metadata"
        )
        bridge_kwargs = hooks._responses_chat_bridge_preemptive_kwargs(
            synthesis_metadata_kwargs,
            original_request,
            include_hosted_web_search_unsupported=True,
            include_client_tool_unsupported=True,
        )

        self.assertIsNone(bridge_kwargs)

    def test_external_web_search_synthesis_uses_selected_upstream_model(self) -> None:
        hooks, _ = load_hook_module()
        original_request = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Original request",
            "stream": True,
            "custom_tools": [{"name": "apply_patch"}],
            "functions": [{"name": "exec_command"}],
            "mcp_servers": [{"name": "computer-use"}],
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY: True,
                hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY: "balanced-chat",
            },
            "model_info": {
                "model_group": "legacy-chat",
                "model": "openai/vendor-chat",
                "route_key": "provider_chat / openai/vendor-chat / key=default",
            },
        }

        synthesis_kwargs = hooks._external_web_search_synthesis_kwargs(
            original_request,
            "Web search results for query: test\n"
            "Title: source\n"
            "URL: https://example.test\n"
            "Snippet: evidence",
        )

        self.assertEqual(synthesis_kwargs["model"], "legacy-chat")
        self.assertNotEqual(synthesis_kwargs["model"], "openai/vendor-chat")

    def test_external_web_search_payloads_do_not_inject_short_timeouts(self) -> None:
        hooks, _ = load_hook_module()
        original_request = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for a source-backed answer.",
            "stream": True,
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY: True,
            },
        }

        synthesis_kwargs = hooks._external_web_search_synthesis_kwargs(
            original_request,
            "Web search results for query: test\n"
            "Title: source\n"
            "URL: https://example.test\n"
            "Snippet: evidence",
        )
        continuation_kwargs = hooks._external_web_search_continuation_kwargs(
            original_request,
            search_results="Web search results for query: test",
            queries=["test"],
            round_number=1,
        )

        for payload in (synthesis_kwargs, continuation_kwargs):
            metadata = payload["litellm_metadata"]
            self.assertNotIn("route_recovery_attempt_timeout_seconds", metadata)
            self.assertNotIn("route_recovery_max_seconds", metadata)
            self.assertEqual(
                hooks._request_timeout_seconds(),
                hooks._REQUEST_TIMEOUT_DEFAULT_SECONDS,
            )
            self.assertEqual(
                hooks._stream_start_timeout_seconds_for_request(payload),
                hooks._STALL_TIMEOUT_DEFAULT_SECONDS,
            )
            self.assertEqual(
                hooks._recovery_max_seconds_for_request(payload),
                hooks._RECOVERY_MAX_DEFAULT_SECONDS,
            )

    def test_external_web_search_low_reasoning_keeps_selected_runtime_credentials(self) -> None:
        hooks, _ = load_hook_module()
        low_kwargs = hooks._external_web_search_low_reasoning_kwargs(
            {
                "call_type": "aresponses",
                "model": "openai/vendor-chat",
                "input": "Use web_search.",
                "stream": True,
                "api_base": "https://chat-provider.example/v1",
                "api_key": "sk-test-route",
                "custom_llm_provider": "openai",
                "litellm_metadata": {
                    hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                },
                "model_info": {
                    "id": "chat-route",
                    "model_group": "legacy-chat",
                    "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
                },
            }
        )

        self.assertEqual(low_kwargs["api_base"], "https://chat-provider.example/v1")
        self.assertEqual(low_kwargs["api_key"], "sk-test-route")
        self.assertEqual(low_kwargs["custom_llm_provider"], "openai")

    def test_external_web_search_continuation_trims_large_source_evidence(self) -> None:
        hooks, _ = load_hook_module()
        large_page = "factor A evidence sentence. " * 600
        raw_results = (
            "Web search results for query: sample subject factor A factor B\n"
            "Title: Transporter source\n"
            "URL: https://example.test/source\n"
            "Snippet: Short search result.\n\n"
            "Retrieved page content for URL: https://example.test/source\n"
            f"Markdown Content:\n{large_page}"
        )

        continuation_kwargs = hooks._external_web_search_continuation_kwargs(
            {
                "call_type": "aresponses",
                "model": "openai/vendor-chat",
                "input": "Deep dive whether sample subject inhibits factor A and factor B.",
                "stream": True,
            },
            search_results=raw_results,
            queries=["sample subject factor A factor B"],
            completed_actions=[
                {"type": "search", "query": "sample subject factor A factor B"},
                {"type": "openPage", "url": "https://example.test/source"},
            ],
            round_number=1,
        )

        evidence = continuation_kwargs["litellm_metadata"][
            "external_web_search_search_results"
        ]
        self.assertLessEqual(
            len(evidence),
            hooks._EXTERNAL_WEB_SEARCH_CONTINUATION_EVIDENCE_MAX_CHARS + 80,
        )
        self.assertLess(len(continuation_kwargs["input"]), len(raw_results))
        self.assertIn("Evidence section trimmed for continuation", evidence)
        self.assertIn("Retrieved evidence observed so far", continuation_kwargs["input"])
        self.assertNotIn(large_page, continuation_kwargs["input"])
        self.assertNotIn(large_page, evidence)
        self.assertTrue(continuation_kwargs["stream"])

    def test_external_web_search_continuation_drops_original_large_instructions(self) -> None:
        hooks, _ = load_hook_module()
        large_instructions = "Repository developer instruction. " * 700

        continuation_kwargs = hooks._external_web_search_continuation_kwargs(
            {
                "call_type": "aresponses",
                "model": "openai/vendor-chat",
                "input": "Use web_search for latest Python.",
                "instructions": large_instructions,
                "stream": True,
            },
            search_results=(
                "Web search results for query: latest Python\n"
                "Title: Python Downloads\n"
                "URL: https://www.python.org/downloads/\n"
                "Snippet: Stable release information."
            ),
            queries=["latest Python"],
            round_number=1,
        )

        self.assertNotIn("Repository developer instruction", continuation_kwargs["instructions"])
        self.assertLess(len(continuation_kwargs["instructions"]), 1400)
        self.assertIn("Decide the next step", continuation_kwargs["input"])
        self.assertIn("Use web_search for latest Python.", continuation_kwargs["input"])

    def test_external_web_search_recovery_prompts_keep_original_user_text_stable(self) -> None:
        hooks, _ = load_hook_module()
        original_request = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": (
                "Compare these two trials carefully https://example.test/one "
                "https://example.test/two"
            ),
            "stream": True,
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY: True,
            },
        }

        continuation_kwargs = hooks._external_web_search_continuation_kwargs(
            original_request,
            search_results=(
                "Web search results for query: trial comparison\n"
                "Title: Trial One\n"
                "URL: https://example.test/one\n"
                "Snippet: Evidence one."
            ),
            queries=["trial comparison"],
            completed_actions=[{"type": "search", "query": "trial comparison"}],
            round_number=1,
        )
        synthesis_kwargs = hooks._external_web_search_synthesis_kwargs(
            continuation_kwargs,
            "Retrieved page content for URL: https://example.test/one\nEvidence one.",
        )
        second_continuation_kwargs = hooks._external_web_search_continuation_kwargs(
            synthesis_kwargs,
            search_results="Retrieved page content for URL: https://example.test/two\nEvidence two.",
            queries=["trial comparison", "https://example.test/two"],
            completed_actions=[
                {"type": "search", "query": "trial comparison"},
                {"type": "openPage", "url": "https://example.test/two"},
            ],
            round_number=2,
        )

        expected = original_request["input"]
        self.assertEqual(
            continuation_kwargs["litellm_metadata"][
                hooks._EXTERNAL_WEB_SEARCH_ORIGINAL_USER_TEXT_KEY
            ],
            expected,
        )
        self.assertEqual(
            synthesis_kwargs["litellm_metadata"][
                hooks._EXTERNAL_WEB_SEARCH_ORIGINAL_USER_TEXT_KEY
            ],
            expected,
        )
        self.assertEqual(
            second_continuation_kwargs["litellm_metadata"][
                hooks._EXTERNAL_WEB_SEARCH_ORIGINAL_USER_TEXT_KEY
            ],
            expected,
        )
        self.assertEqual(
            hooks._external_web_search_user_prompt_text(second_continuation_kwargs),
            expected,
        )
        self.assertNotIn("Original user request: Original user request", synthesis_kwargs["input"])
        self.assertNotIn("Original user request: Original user request", second_continuation_kwargs["input"])
        self.assertNotIn("Retrieved evidence observed so far", synthesis_kwargs["input"].split(expected, 1)[0])
        self.assertIn(expected, synthesis_kwargs["input"])
        self.assertIn(expected, second_continuation_kwargs["input"])

        legacy_recovery_request = {
            "input": (
                "Original user request. Any instruction to call or use web_search has "
                "already been satisfied by the compatibility bridge:\n"
                "Original user request:\n"
                f"Original user request:\n{expected}\n\n"
                "Web actions completed so far:\n- trial comparison\n\n"
                "Retrieved evidence observed so far:\nEvidence from a previous round.\n\n"
                "Decide the next step now: call the web search bridge function.\n\n"
                "Retrieved evidence:\nEvidence from synthesis.\n\n"
                "Now answer the original user request directly. Do not call tools."
            )
        }
        self.assertEqual(
            hooks._external_web_search_user_prompt_text(legacy_recovery_request),
            expected,
        )

    def test_external_web_search_synthesis_keeps_current_bridge_model_without_model_info_model(self) -> None:
        hooks, _ = load_hook_module()
        original_request = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Original request",
            "stream": True,
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY: True,
                hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY: "balanced-chat",
            },
            "model_info": {
                "id": "79f0dc70",
                "model_group": "legacy-chat",
                "route_key": "provider_chat / openai/vendor-chat / key=default",
            },
        }

        synthesis_kwargs = hooks._external_web_search_synthesis_kwargs(
            original_request,
            "Web search results for query: test\n"
            "Title: source\n"
            "URL: https://example.test\n"
            "Snippet: evidence",
        )
        continuation_kwargs = hooks._external_web_search_continuation_kwargs(
            original_request,
            search_results="Web search results for query: test",
            queries=["test"],
            round_number=1,
        )

        self.assertEqual(synthesis_kwargs["model"], "legacy-chat")
        self.assertEqual(continuation_kwargs["model"], "legacy-chat")
        self.assertNotEqual(synthesis_kwargs["model"], "openai/vendor-chat")
        self.assertNotEqual(continuation_kwargs["model"], "openai/vendor-chat")
        self.assertEqual(
            [tool.get("name") for tool in continuation_kwargs["tools"]],
            [hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME],
        )
        continuation_surface = json.dumps(
            {
                "tools": continuation_kwargs["tools"],
                "instructions": continuation_kwargs["instructions"],
                "input": continuation_kwargs["input"],
            }
        )
        self.assertIn("url", continuation_surface)
        self.assertIn("pattern", continuation_surface)
        self.assertNotIn("custom_tools", continuation_kwargs)
        self.assertNotIn("functions", continuation_kwargs)
        self.assertNotIn("mcp_servers", continuation_kwargs)

    def test_external_web_search_router_model_group_reads_new_route_key_model(self) -> None:
        hooks, _ = load_hook_module()

        self.assertEqual(
            hooks._external_web_search_router_model_group(
                {
                    "model_info": {
                        "route_key": "model=llmwebsearch / provider=openrouter / upstream=openai/vendor/vendor-chat / host=openrouter.ai / key=default / order=1",
                    },
                }
            ),
            "llmwebsearch",
        )
        self.assertIsNone(
            hooks._external_web_search_router_model_group(
                {
                    "model_info": {
                        "route_key": "provider=openrouter / upstream=openai/vendor/vendor-chat / host=openrouter.ai / key=default / order=1",
                    },
                }
            )
        )

    def test_external_web_search_recovery_payloads_prefer_selected_route_over_outer_alias(self) -> None:
        hooks, _ = load_hook_module()
        original_request = {
            "call_type": "aresponses",
            "model": "legacy-chat",
            "input": "Original request",
            "stream": True,
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY: True,
                hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY: "legacy-chat",
            },
            "model_info": {
                "id": "79f0dc70",
                "model_group": "legacy-chat",
                "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
            },
            "litellm_params": {
                "model": "openai/vendor-chat",
                "api_base": "https://chat-provider.example/v1",
                "order": 1,
            },
        }

        synthesis_kwargs = hooks._external_web_search_synthesis_kwargs(
            original_request,
            "Web search results for query: test\n"
            "Title: source\n"
            "URL: https://example.test\n"
            "Snippet: evidence",
        )
        continuation_kwargs = hooks._external_web_search_continuation_kwargs(
            original_request,
            search_results="Web search results for query: test",
            queries=["test"],
            round_number=1,
        )

        self.assertEqual(synthesis_kwargs["model"], "legacy-chat")
        self.assertEqual(continuation_kwargs["model"], "legacy-chat")
        self.assertEqual(continuation_kwargs["max_output_tokens"], 512)
        self.assertTrue(continuation_kwargs["stream"])
        self.assertEqual(
            continuation_kwargs["litellm_params"]["api_base"],
            "https://chat-provider.example/v1",
        )
        self.assertNotEqual(synthesis_kwargs["model"], "openai/vendor-chat")
        self.assertNotEqual(continuation_kwargs["model"], "openai/vendor-chat")

    def test_external_web_search_continuation_keeps_explicit_output_budget(self) -> None:
        hooks, _ = load_hook_module()
        continuation_kwargs = hooks._external_web_search_continuation_kwargs(
            {
                "model": "openai/vendor-chat",
                "input": "Use web_search.",
                "max_output_tokens": 300,
            },
            search_results="Web search results for query: test",
            queries=["test"],
            round_number=1,
        )

        self.assertEqual(continuation_kwargs["max_output_tokens"], 300)

    def test_external_web_search_continuation_recovery_drops_noncopyable_request_internals(self) -> None:
        hooks, _ = load_hook_module()
        ssl_context = ssl.create_default_context()
        original_request = {
            "call_type": "aresponses",
            "model": "legacy-chat",
            "input": "Use web_search for latest Python.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "ssl_context": ssl_context,
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY: "legacy-chat",
                "proxy_server_request": {
                    "url": "http://127.0.0.1:4000/v1/responses",
                    "ssl_context": ssl_context,
                },
            },
            "model_info": {
                "id": "79f0dc70",
                "model_group": "legacy-chat",
                "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
            },
            "litellm_params": {
                "model": "openai/vendor-chat",
                "api_base": "https://example.test/v1",
                "ssl_context": ssl_context,
            },
        }

        continuation_kwargs = hooks._external_web_search_prepare_continuation_recovery_request(
            request_kwargs=original_request,
            search_results=(
                "Web search results for query: latest Python\n"
                "Title: Python Downloads\n"
                "URL: https://www.python.org/downloads/\n"
                "Snippet: Stable release information."
            ),
            source_urls=["https://www.python.org/downloads/"],
            queries=["latest Python"],
            completed_actions=[{"type": "search", "query": "latest Python"}],
            round_number=1,
        )
        pending = hooks._external_web_search_pending_recovery_request(original_request)

        copy.deepcopy(continuation_kwargs)
        copy.deepcopy(pending)
        json.dumps(continuation_kwargs)
        json.dumps(pending)
        self.assertEqual(continuation_kwargs["model"], "legacy-chat")
        self.assertEqual(
            continuation_kwargs["litellm_params"]["api_base"],
            "https://example.test/v1",
        )
        self.assertTrue(
            continuation_kwargs["litellm_metadata"]["external_web_search_continuation"]
        )
        dumped = json.dumps({"continuation": continuation_kwargs, "pending": pending})
        self.assertNotIn("proxy_server_request", dumped)
        self.assertNotIn("ssl_context", dumped)
        self.assertNotIn("sslcontext", dumped)

    def test_external_web_search_recovery_keeps_selected_route_over_outer_alias(self) -> None:
        hooks, _ = load_hook_module()

        class GatewayTimeout(Exception):
            status_code = 504

        recovery_request = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Retrieved evidence observed so far:\nshort evidence",
            "stream": False,
            "litellm_metadata": {
                "external_web_search_continuation": True,
                hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY: "legacy-chat",
            },
            "model_info": {
                "id": "79f0dc70",
                "model_group": "legacy-chat",
                "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
            },
        }
        exc = GatewayTimeout("upstream 504")
        hooks._external_web_search_set_recovery_request(exc, recovery_request)

        payload = hooks._external_web_search_recovery_kwargs(
            {
                "model": "legacy-chat",
                "litellm_metadata": {
                    hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY: "legacy-chat",
                },
            },
            exception=exc,
        )

        self.assertEqual(payload["model"], "legacy-chat")
        self.assertNotEqual(payload["model"], "openai/vendor-chat")
        self.assertTrue(payload["stream"])
        self.assertNotIn("route_recovery_max_seconds", payload["litellm_metadata"])
