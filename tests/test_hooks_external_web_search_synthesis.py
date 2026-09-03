from __future__ import annotations

import copy

from hook_test_utils import *


class HookExternalWebSearchSynthesisTests(HookTestCase):
    def test_pi_web_access_page_reader_uses_worker_response(self) -> None:
        hooks, _ = load_hook_module()
        original_worker = hooks._pi_web_access_module._worker_request
        observed = {}

        def fake_worker(request, *, timeout_seconds, max_results=None):
            observed["request"] = request
            observed["timeout_seconds"] = timeout_seconds
            observed["max_results"] = max_results
            return {
                "ok": True,
                "text": (
                    "Retrieved page content for URL: "
                    "https://raw.githubusercontent.com/example/project/main/source.py\n\n"
                    "Source title\nsource file text"
                ),
                "sourceUrls": [
                    "https://raw.githubusercontent.com/example/project/main/source.py"
                ],
                "details": {},
            }

        hooks._pi_web_access_module._worker_request = fake_worker
        self.addCleanup(
            setattr,
            hooks._pi_web_access_module,
            "_worker_request",
            original_worker,
        )

        text = hooks._pi_web_access_page_excerpt(
            "https://raw.githubusercontent.com/example/project/main/source.py",
            timeout=12,
            max_chars=200,
        )

        self.assertEqual(text, "Source title\nsource file text")
        self.assertEqual(
            observed["request"],
            {
                "action": "openPage",
                "url": "https://raw.githubusercontent.com/example/project/main/source.py",
            },
        )
        self.assertEqual(observed["timeout_seconds"], 12)
        self.assertIsNone(observed["max_results"])

    def test_pi_web_access_search_returns_structured_sources(self) -> None:
        hooks, _ = load_hook_module()
        original_worker = hooks._pi_web_access_module._worker_request
        observed = {}

        def fake_worker(request, *, timeout_seconds, max_results=None):
            observed["request"] = request
            observed["timeout_seconds"] = timeout_seconds
            observed["max_results"] = max_results
            return {
                "ok": True,
                "text": "1. Signal source https://example.test/erk",
                "sourceUrls": ["https://example.test/erk"],
                "details": {"provider": "test"},
            }

        hooks._pi_web_access_module._worker_request = fake_worker
        self.addCleanup(
            setattr,
            hooks._pi_web_access_module,
            "_worker_request",
            original_worker,
        )

        text, structured = hooks._pi_web_access_search_sync("sample subject Signal")

        self.assertIn("Signal source", text)
        self.assertEqual(structured["results"], [{"url": "https://example.test/erk"}])
        self.assertEqual(structured["details"], {"provider": "test"})
        self.assertEqual(
            observed["request"],
            {"action": "search", "query": "sample subject Signal", "page": 1},
        )
        self.assertEqual(observed["max_results"], 5)

    def test_pi_web_access_search_forwards_result_pages(self) -> None:
        hooks, _ = load_hook_module()
        original_worker = hooks._pi_web_access_module._worker_request
        observed = {}

        def fake_worker(request, *, timeout_seconds, max_results=None):
            observed["request"] = request
            return {
                "ok": True,
                "text": "2. Search result https://example.test/page-2",
                "sourceUrls": ["https://example.test/page-2"],
                "details": {},
            }

        hooks._pi_web_access_module._worker_request = fake_worker
        self.addCleanup(
            setattr,
            hooks._pi_web_access_module,
            "_worker_request",
            original_worker,
        )

        hooks._pi_web_access_search_sync("safe query", page=2)
        self.assertEqual(
            observed["request"],
            {"action": "search", "query": "safe query", "page": 2},
        )

    def test_external_web_search_defaults_bound_fast_common_queries(self) -> None:
        hooks, _ = load_hook_module()

        self.assertEqual(hooks._EXTERNAL_WEB_SEARCH_MAX_RESULTS_DEFAULT, 5)
        self.assertEqual(hooks._EXTERNAL_WEB_FETCH_TIMEOUT_DEFAULT, 12.0)
        self.assertEqual(hooks._EXTERNAL_WEB_SEARCH_MAX_ROUNDS_DEFAULT, 8)
        self.assertEqual(hooks._DEFAULT_MAX_RESULTS, 5)
        self.assertEqual(hooks._DEFAULT_FETCH_TIMEOUT_SECONDS, 12.0)

    async def test_external_web_search_synthesis_failure_raises_for_route_recovery(self) -> None:
        hooks, _ = load_hook_module()

        class TemporarySynthesisError(Exception):
            status_code = 504

        async def failing_original(**_kwargs):
            raise TemporarySynthesisError("route group unavailable")

        with self.assertRaises(TemporarySynthesisError) as context:
            await hooks._external_web_search_synthesize_or_fallback(
                request_kwargs={"model": "balanced-chat"},
                search_results=(
                    "Web search results for query: test\n"
                    "Title: Source One\n"
                    "URL: https://example.test/one\n"
                    "Snippet: useful snippet.\n\n"
                    "Retrieved page content for URL: https://example.test/one\n"
                    "\n"
                    "raw page body that should not be copied wholesale"
                ),
                queries=["test"],
                source_urls=["https://example.test/one"],
                original_function=failing_original,
            )

        recovery_request = hooks._external_web_search_recovery_request_from_exception(
            context.exception
        )
        self.assertIsInstance(recovery_request, dict)
        self.assertTrue(recovery_request.get("litellm_metadata", {}).get("external_web_search_synthesis"))
        self.assertIn("Retrieved evidence", recovery_request.get("input", ""))
        self.assertNotIn("Source One", str(context.exception))
        self.assertNotIn("https://example.test/one", str(context.exception))
        self.assertNotIn("raw page body that should not be copied wholesale", str(context.exception))

    async def test_external_web_search_initial_structured_message_is_terminal(self) -> None:
        hooks, _ = load_hook_module()
        response = {
            "id": "resp_preamble",
            "object": "response",
            "status": "completed",
            "output_text": "我来为你深挖调查 sample compound 与supportive medication的联用问题。先调用 data-source helper 抽取数据源速查，再执行全网检索和跨源核验。",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "我来为你深挖调查 sample compound 与supportive medication的联用问题。先调用 data-source helper 抽取数据源速查，再执行全网检索和跨源核验。",
                        }
                    ],
                }
            ],
        }

        resolved = await hooks._resolve_web_search_function_calls(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "深挖调查 sample compound 与哪些supportive medication可以联用。",
                "tools": [{"type": "web_search"}],
            },
            original_function=None,
        )

        self.assertIs(resolved, response)

    async def test_external_web_search_initial_no_tool_direct_answer_is_allowed(self) -> None:
        hooks, _ = load_hook_module()
        response = {
            "id": "resp_answer",
            "object": "response",
            "status": "completed",
            "output_text": "可以联用，但需要避免强 CYP3A 抑制剂并监测 QT 风险。",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "可以联用，但需要避免强 CYP3A 抑制剂并监测 QT 风险。",
                        }
                    ],
                }
            ],
        }

        resolved = await hooks._resolve_web_search_function_calls(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "sample compound 与supportive medication是否可以联用？",
                "tools": [{"type": "web_search"}],
            },
            original_function=None,
        )

        self.assertIs(resolved, response)

    async def test_external_web_search_malformed_initial_tool_call_is_rejected(self) -> None:
        hooks, _ = load_hook_module()
        response = {
            "id": "resp_truncated_tool_call",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": '{"query":"',
                    "status": "completed",
                }
            ],
        }

        with self.assertRaises(RuntimeError) as context:
            await hooks._resolve_web_search_function_calls(
                response,
                {
                    "model": "openai/vendor-chat",
                    "input": "Use web_search for Sample City weather.",
                    "tools": [{"type": "web_search"}],
                },
                original_function=None,
            )

        self.assertEqual(getattr(context.exception, "status_code", None), 503)
        self.assertIn("malformed_web_search_function_call", str(context.exception))

    async def test_external_web_search_chat_tool_retries_one_malformed_call(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def acompletion(self, **kwargs):
                calls.append(copy.deepcopy(kwargs))
                arguments = '{"query":"' if len(calls) == 1 else json.dumps(
                    {"query": "Sample City current weather"}
                )
                return {
                    "id": f"chat_tool_{len(calls)}",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": f"call_{len(calls)}",
                                        "type": "function",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": arguments,
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                }

        proxy_server.llm_router = FakeRouter()
        call_kwargs = {
            "model": "openai/vendor-chat",
            "input": "Use web_search for Sample City weather.",
            "tools": [hooks._pi_web_access_tool_definitions()[0]],
            "model_info": {
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        }

        response = await hooks._external_web_search_chat_tool_response(
            call_kwargs,
            call_kwargs,
            phase="initial",
        )

        self.assertEqual(len(calls), 2)
        self.assertIn("malformed or truncated", calls[1]["messages"][0]["content"])
        self.assertEqual(
            hooks._web_search_actions_from_response(response),
            [{"type": "search", "query": "Sample City current weather"}],
        )

    def test_external_web_search_initial_current_fact_forces_only_first_lookup(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "model": "openai/vendor-chat",
            "input": "请查上海今天天气。",
            "tools": [
                hooks._pi_web_access_tool_definitions()[0],
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {"type": "object"},
                },
            ],
            "model_info": {
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        }

        initial = hooks._external_web_search_chat_tool_payload(
            request_kwargs,
            request_kwargs,
            phase="initial",
        )
        continuation = hooks._external_web_search_chat_tool_payload(
            request_kwargs,
            request_kwargs,
            phase="continuation",
        )

        self.assertEqual(initial["tool_choice"], "auto")
        self.assertNotIn("tool_choice", continuation)

    def test_external_web_search_initial_current_fact_keeps_kimi_search_optional(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "model": "openai/kimi-k3",
            "input": "请查上海今天天气。",
            "reasoning": {"effort": "low"},
            "tools": [hooks._pi_web_access_tool_definitions()[0]],
            "model_info": {
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        }

        payload = hooks._external_web_search_chat_tool_payload(
            request_kwargs,
            request_kwargs,
            phase="initial",
        )

        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(
            [tool["function"]["name"] for tool in payload["tools"]],
            ["web_search"],
        )
        self.assertEqual(payload["max_completion_tokens"], 512)
        self.assertIn("optional", payload["messages"][0]["content"])

    def test_external_web_search_non_gpt_current_fact_keeps_search_optional(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "model": "openai/kimi-k3",
            "input": "上海前天天气",
            "reasoning": {"effort": "low"},
            "tools": [hooks._pi_web_access_tool_definitions()[0]],
            "model_info": {
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
                "model": "openai/kimi-k3",
            },
        }

        payload = hooks._external_web_search_chat_tool_payload(
            request_kwargs,
            request_kwargs,
            phase="initial",
        )

        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(payload["max_completion_tokens"], 512)
        self.assertIn("optional", payload["messages"][0]["content"])

    def test_external_web_search_continuation_slims_codex_deferred_tools(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "model": "openai/vendor-chat",
            "input": "上海前天天气",
            "tools": [
                {"type": "tool_search"},
                {
                    "type": "namespace",
                    "name": "codex_app",
                    "tools": [{"type": "function", "name": "read_thread"}],
                },
            ],
        }

        continuation_kwargs = hooks._external_web_search_continuation_kwargs(
            request_kwargs,
            search_results="Web search results for query: 上海前天天气",
            queries=["上海前天天气"],
            completed_actions=[{"type": "search", "query": "上海前天天气"}],
            round_number=1,
        )

        self.assertEqual(
            continuation_kwargs["tools"],
            hooks._pi_web_access_tool_definitions(),
        )
        self.assertNotIn("max_output_tokens", continuation_kwargs)

    def test_external_web_search_continuation_slims_flattened_codex_tool_search(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "model": "openai/vendor-chat",
            "input": "上海前天天气",
            "tools": [
                {
                    "type": "function",
                    "name": "tool_search",
                    "parameters": {"type": "object"},
                },
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {"type": "object"},
                },
            ],
        }

        self.assertEqual(
            hooks._external_web_search_continuation_tools(request_kwargs),
            hooks._pi_web_access_tool_definitions(),
        )

    def test_external_web_search_continuation_keeps_native_pi_tools_without_legacy_bridge(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "model": "openai/vendor-responses",
            "input": "上海今天天气",
            "tools": [
                {"type": "function", "name": "web_search", "parameters": {"type": "object"}},
                {"type": "function", "name": "fetch_content", "parameters": {"type": "object"}},
            ],
        }

        continuation = hooks._external_web_search_continuation_kwargs(
            request_kwargs,
            search_results="Web search results for query: 上海今天天气",
            queries=["上海今天天气"],
            completed_actions=[{"type": "search", "query": "上海今天天气"}],
            round_number=1,
        )

        self.assertEqual(
            {tool.get("name") for tool in continuation["tools"]},
            {"web_search", "fetch_content"},
        )
        self.assertNotIn("private_web_search_bridge", continuation["instructions"])
        self.assertNotIn("private lookup function", continuation["input"])

    async def test_external_web_search_continuation_keeps_active_chat_surface(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        test_case = self

        class FakeRouter:
            async def acompletion(self, **kwargs):
                calls.append(copy.deepcopy(kwargs))
                selected_box = hooks._CURRENT_SELECTED_DEPLOYMENT_BOX.get()
                test_case.assertIsInstance(selected_box, dict)
                selected_box["marker"] = {
                    "model_info": {
                        "id": "selected-chat-route",
                        "model_group": "mapped-chat",
                        "model": "openai/vendor-chat",
                    },
                    "litellm_params": {"model": "openai/vendor-chat"},
                    "request_params": {},
                    "_litellm_menu_upstream_url_surface": "openai/chat",
                }
                if len(calls) == 1:
                    return {
                        "id": "chat_initial",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "call_initial",
                                            "type": "function",
                                            "function": {
                                                "name": "web_search",
                                                "arguments": json.dumps(
                                                    {"query": "上海天气 今天"}
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ],
                    }
                return {
                    "id": "chat_continuation",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "上海天气信息已从检索结果中整理。",
                            }
                        }
                    ],
                }

        async def original_function(**_kwargs):
            raise AssertionError("continuation must retain the active chat route")

        proxy_server.llm_router = FakeRouter()
        active_bridge_request = {
            "call_type": "aresponses",
            "model": "mapped-chat",
            "input": "查询上海今天天气。",
            "stream": True,
            "use_chat_completions_api": True,
            "_litellm_menu_upstream_url_surface": "openai/chat",
            "tools": [hooks._pi_web_access_tool_definitions()[0]],
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY: True,
            },
        }

        initial_call_kwargs = hooks._with_external_web_search_post_call_suppressed(
            active_bridge_request
        )
        initial = await hooks._external_web_search_chat_tool_response(
            initial_call_kwargs,
            active_bridge_request,
            phase="initial",
        )
        self.assertEqual(
            hooks._web_search_actions_from_response(initial),
            [{"type": "search", "query": "上海天气 今天"}],
        )
        self.assertEqual(
            active_bridge_request["_litellm_menu_upstream_url_surface"],
            "openai/chat",
        )
        self.assertEqual(
            active_bridge_request["model_info"]["id"],
            "selected-chat-route",
        )

        response = await hooks._external_web_search_continue_or_synthesize(
            request_kwargs=active_bridge_request,
            search_results=(
                "Web search results for query: 上海天气 今天\n"
                "Title: 上海天气\n"
                "URL: https://example.test/weather/shanghai\n"
                "Snippet: 晴，气温信息。"
            ),
            queries=["上海天气 今天"],
            completed_actions=[{"type": "search", "query": "上海天气 今天"}],
            source_urls=["https://example.test/weather/shanghai"],
            round_number=1,
            original_function=original_function,
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["model"], "mapped-chat")
        self.assertIn(
            "上海天气信息已从检索结果中整理。",
            hooks._responses_output_module._response_text(response),
        )

    async def test_external_web_search_chat_progress_retries_into_custom_tool_call(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def acompletion(self, **kwargs):
                calls.append(copy.deepcopy(kwargs))
                if len(calls) == 1:
                    return {
                        "id": "chat_plan",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": (
                                        "我会用 `$research` 技能来完成这个任务。"
                                        "先读取技能说明。"
                                    ),
                                }
                            }
                        ],
                    }
                return {
                    "id": "chat_tool",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_read_skill",
                                        "type": "function",
                                        "function": {
                                            "name": "exec",
                                            "arguments": json.dumps(
                                                {
                                                    "input": (
                                                        "text(await tools.exec_command({"
                                                        "cmd: 'sed -n \\\"1,80p\\\" SKILL.md'"
                                                        "}));"
                                                    )
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                }

        proxy_server.llm_router = FakeRouter()
        bridge_tools, _options, _stats = hooks._responses_chat_bridge_sanitize_tools(
            [
                {
                    "type": "custom",
                    "name": "exec",
                    "description": "Run JavaScript in the task runtime.",
                },
                hooks._pi_web_access_tool_definitions()[0],
            ],
            bridge_web_search=True,
        )
        request_kwargs = {
            "call_type": "aresponses",
            "model": "mapped-chat",
            "input": "Investigate the claim with the required skill.",
            "stream": True,
            "_litellm_menu_upstream_url_surface": "openai/chat",
            "tools": bridge_tools,
        }

        response = await hooks._external_web_search_chat_tool_response(
            hooks._with_external_web_search_post_call_suppressed(request_kwargs),
            request_kwargs,
            phase="initial",
        )

        self.assertEqual(len(calls), 2)
        self.assertIn("先读取技能说明", calls[1]["messages"][-2]["content"])
        self.assertIn("Do not return another status update", calls[1]["messages"][-1]["content"])
        self.assertEqual(response.get("output_text"), None)
        self.assertEqual(len(response["output"]), 1)
        tool_call = response["output"][0]
        self.assertEqual(tool_call["type"], "custom_tool_call")
        self.assertEqual(tool_call["name"], "exec")
        self.assertIn("exec_command", tool_call["input"])

    def test_external_web_search_chat_messages_preserve_tool_history(self) -> None:
        hooks, _ = load_hook_module()

        messages = hooks._external_web_search_chat_tool_messages(
            {
                "input": [
                    {"role": "user", "content": "Read the skill, then continue."},
                    {
                        "type": "function_call",
                        "id": "call_read",
                        "call_id": "call_read",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "sed -n '1,30p' SKILL.md"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_read",
                        "output": "Skill instructions were read.",
                    },
                    {
                        "type": "custom_tool_call",
                        "id": "ctc_apply",
                        "call_id": "ctc_apply",
                        "name": "exec",
                        "input": "text(await tools.apply_patch(patch));",
                    },
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "ctc_apply",
                        "output": "Patch applied.",
                    },
                ]
            }
        )

        self.assertEqual(messages[0], {"role": "user", "content": "Read the skill, then continue."})
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[1]["tool_calls"][0]["id"], "call_read")
        self.assertEqual(messages[1]["tool_calls"][0]["function"]["name"], "exec_command")
        self.assertEqual(messages[2], {
            "role": "tool",
            "tool_call_id": "call_read",
            "content": "Skill instructions were read.",
        })
        self.assertEqual(messages[3]["role"], "assistant")
        self.assertEqual(messages[3]["tool_calls"][0]["function"]["name"], "exec")
        self.assertEqual(
            json.loads(messages[3]["tool_calls"][0]["function"]["arguments"]),
            {"input": "text(await tools.apply_patch(patch));"},
        )
        self.assertEqual(messages[4], {
            "role": "tool",
            "tool_call_id": "ctc_apply",
            "content": "Patch applied.",
        })

    async def test_external_web_search_chat_tool_replays_tool_history(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def acompletion(self, **kwargs):
                calls.append(copy.deepcopy(kwargs))
                return {
                    "id": "chat_followup",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "The prior tool result is enough to continue.",
                            }
                        }
                    ],
                }

        proxy_server.llm_router = FakeRouter()
        request_kwargs = {
            "model": "openai/vendor-chat",
            "input": [
                {"role": "user", "content": "Continue the investigation."},
                {
                    "type": "function_call",
                    "id": "call_read",
                    "call_id": "call_read",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "sed -n '1,30p' SKILL.md"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_read",
                    "output": "Skill content.",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {"type": "object"},
                },
                hooks._pi_web_access_tool_definitions()[0],
            ],
            "model_info": {
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        }

        response = await hooks._external_web_search_chat_tool_response(
            request_kwargs,
            request_kwargs,
            phase="initial",
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["messages"][1]["role"], "assistant")
        self.assertEqual(calls[0]["messages"][1]["tool_calls"][0]["id"], "call_read")
        self.assertEqual(calls[0]["messages"][2], {
            "role": "tool",
            "tool_call_id": "call_read",
            "content": "Skill content.",
        })
        self.assertIn("prior tool result", hooks._responses_output_module._response_text(response))

    async def test_external_web_search_synthesis_recovery_trims_large_metadata_evidence(self) -> None:
        hooks, _ = load_hook_module()
        large_page = "full page transporter detail. " * 700

        class TemporarySynthesisError(Exception):
            status_code = 504

        async def failing_original(**_kwargs):
            raise TemporarySynthesisError("route group unavailable")

        with self.assertRaises(TemporarySynthesisError) as context:
            await hooks._external_web_search_synthesize_or_fallback(
                request_kwargs={"model": "openai/vendor-chat"},
                search_results=(
                    "Web search results for query: sample subject factor A factor B\n"
                    "Title: Transporter source\n"
                    "URL: https://example.test/source\n"
                    "Snippet: Short search result.\n\n"
                    "Retrieved page content for URL: https://example.test/source\n"
                    f"Markdown Content:\n{large_page}"
                ),
                queries=["sample subject factor A factor B"],
                source_urls=["https://example.test/source"],
                original_function=failing_original,
            )

        recovery_request = hooks._external_web_search_recovery_request_from_exception(
            context.exception
        )
        self.assertIsInstance(recovery_request, dict)
        evidence = recovery_request["litellm_metadata"][
            "external_web_search_search_results"
        ]
        self.assertLessEqual(
            len(evidence),
            hooks._EXTERNAL_WEB_SEARCH_SYNTHESIS_EVIDENCE_MAX_CHARS + 80,
        )
        self.assertIn("Evidence section trimmed for synthesis", evidence)
        self.assertNotIn(large_page, recovery_request.get("input", ""))
        self.assertNotIn(large_page, evidence)

    async def test_external_web_search_missing_original_function_raises_without_dumping_results(self) -> None:
        hooks, _ = load_hook_module()

        with self.assertRaises(Exception) as context:
            await hooks._external_web_search_synthesize_or_fallback(
                request_kwargs={"model": "balanced-chat"},
                search_results=(
                    "Web search results for query: test\n"
                    "Title: Source One\n"
                    "URL: https://example.test/one\n"
                    "Snippet: useful snippet.\n\n"
                    "raw page body that should not be copied wholesale"
                ),
                queries=["test"],
                source_urls=["https://example.test/one"],
                original_function=None,
            )

        text = str(context.exception)
        self.assertIn("missing_original_function", text)
        self.assertNotIn("Source One", text)
        self.assertNotIn("https://example.test/one", text)
        self.assertNotIn("useful snippet", text)
        self.assertNotIn("raw page body that should not be copied wholesale", text)

    async def test_external_web_search_synthesis_forces_low_reasoning(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_function(**kwargs):
            calls.append(kwargs)
            return {
                "id": "resp_synthesized",
                "object": "response",
                "status": "completed",
                "output_text": "done",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            }

        response = await hooks._external_web_search_synthesize_or_fallback(
            request_kwargs={
                "model": "balanced-chat",
                "input": "Use web_search.",
                "reasoning": {"effort": "xhigh"},
                "model_info": {
                    "id": "79f0dc70",
                    "route_key": "provider_chat / openai/vendor-chat / key=default",
                },
            },
            search_results=(
                "Web search results for query: test\n"
                "Title: Source One\n"
                "URL: https://example.test/one\n"
                "Snippet: useful snippet."
            ),
            queries=["test"],
            source_urls=["https://example.test/one"],
            original_function=original_function,
        )

        self.assertEqual(response.get("output_text"), "done")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["reasoning"]["effort"], "low")
        self.assertEqual(calls[0].get("max_output_tokens"), 1536)

    async def test_external_web_search_synthesis_uses_chat_messages_for_chat_only_route(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def acompletion(self, **kwargs):
                calls.append(kwargs)
                return {
                    "id": "chat_synthesized",
                    "object": "chat.completion",
                    "model": "legacy-chat",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "Chat synthesis answer. https://example.test/one",
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }

        proxy_server.llm_router = FakeRouter()

        async def original_function(**_kwargs):
            raise AssertionError("chat-only synthesis should not use responses input")

        response = await hooks._external_web_search_synthesize_or_fallback(
            request_kwargs={
                "model": "openai/vendor-chat",
                "input": "Use web_search.",
                "reasoning": {"effort": "xhigh"},
                "model_info": {
                    "id": "chatroute",
                    "route_key": "provider_chat / openai/vendor-chat / key=default",
                    "upstream_url_surface": "openai/chat",
                    "supported_upstream_url_surfaces": ["openai/chat"],
                },
            },
            search_results=(
                "Web search results for query: test\n"
                "Title: Source One\n"
                "URL: https://example.test/one\n"
                "Snippet: useful snippet."
            ),
            queries=["test"],
            source_urls=["https://example.test/one"],
            original_function=original_function,
        )

        self.assertEqual(response.get("output_text"), "Chat synthesis answer. https://example.test/one")
        self.assertEqual(response.get("id"), "chat_synthesized")
        self.assertEqual(response.get("usage", {}).get("total_tokens"), 15)
        self.assertEqual(len(calls), 1)
        payload = calls[0]
        self.assertEqual(payload.get("model"), "openai/vendor-chat")
        self.assertEqual(payload.get("stream"), False)
        self.assertEqual(payload.get("reasoning"), {"effort": "low"})
        self.assertEqual(payload.get("max_completion_tokens"), 1536)
        self.assertNotIn("max_output_tokens", payload)
        self.assertNotIn("input", payload)
        self.assertNotIn("instructions", payload)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["role"], "user")
        self.assertIn("External web_search compatibility bridge synthesis mode", payload["messages"][0]["content"])
        self.assertIn("Retrieved evidence", payload["messages"][1]["content"])

        def assert_no_timeout_keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn("timeout", str(key).lower())
                    assert_no_timeout_keys(child)
            elif isinstance(value, list):
                for child in value:
                    assert_no_timeout_keys(child)

        assert_no_timeout_keys(payload)

    async def test_external_web_search_chat_synthesis_reads_nonstandard_chat_content(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def acompletion(self, **_kwargs):
                return {
                    "id": "chat_nonstandard",
                    "object": "chat.completion",
                    "model": "legacy-chat",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": "Evidence answer. "},
                                    {
                                        "delta": {
                                            "content": "https://example.test/one"
                                        }
                                    },
                                ],
                            },
                        }
                    ],
                }

        proxy_server.llm_router = FakeRouter()

        async def original_function(**_kwargs):
            raise AssertionError("chat-only synthesis should not fall back to responses")

        response = await hooks._external_web_search_synthesize_or_fallback(
            request_kwargs={
                "model": "openai/vendor-chat",
                "input": "Use web_search.",
                "model_info": {
                    "upstream_url_surface": "openai/chat",
                    "supported_upstream_url_surfaces": ["openai/chat"],
                },
            },
            search_results=(
                "Web search results for query: test\n"
                "Title: Source One\n"
                "URL: https://example.test/one\n"
                "Snippet: useful snippet."
            ),
            queries=["test"],
            source_urls=["https://example.test/one"],
            original_function=original_function,
        )

        self.assertEqual(
            response.get("output_text"),
            "Evidence answer. https://example.test/one",
        )

    def test_external_web_search_synthesis_respects_larger_output_budget(self) -> None:
        hooks, _ = load_hook_module()

        kwargs = hooks._external_web_search_synthesis_kwargs(
            {
                "model": "legacy-chat",
                "input": "Use web_search.",
                "max_output_tokens": 2048,
            },
            (
                "Web search results for query: test\n"
                "Title: Source One\n"
                "URL: https://example.test/one\n"
                "Snippet: useful snippet."
            ),
        )

        self.assertEqual(kwargs.get("max_output_tokens"), 2048)

    def test_external_web_search_synthesis_prioritizes_model_opened_page_evidence(self) -> None:
        hooks, _ = load_hook_module()
        search_sections = "\n\n".join(
            [
                (
                    "Web search results for query: earlier summary\n"
                    "Title: Wrapper\n"
                    "URL: https://example.test/wrapper\n"
                    f"Snippet: {'old summary. ' * 180}"
                ),
                (
                    "Retrieved page content for URL: https://example.test/source\n\n"
                    f"Official source: {'verified page text. ' * 180}"
                ),
                (
                    "Page text matches for pattern: page\n"
                    "URL: https://example.test/source\n\n"
                    "- The official signature includes page: int = 1."
                ),
            ]
        )

        evidence = hooks._external_web_search_synthesis_evidence(search_sections)

        self.assertIn("The official signature includes page: int = 1.", evidence)
        self.assertIn("Official source:", evidence)
        self.assertLess(
            evidence.index("The official signature includes page: int = 1."),
            evidence.index("Web search results for query: earlier summary"),
        )

    def test_external_web_search_synthesis_tells_model_how_to_weigh_opened_evidence(self) -> None:
        hooks, _ = load_hook_module()

        kwargs = hooks._external_web_search_synthesis_kwargs(
            {"model": "legacy-chat", "input": "Use web_search."},
            "Retrieved page content for URL: https://example.test/source\n\nOfficial text.",
        )

        self.assertIn("opened-page content are stronger evidence", kwargs["instructions"])

    async def test_external_web_search_synthesis_trims_page_evidence_for_recovery(self) -> None:
        hooks, _ = load_hook_module()
        calls = []
        long_page = "A" * 12000

        async def original_function(**kwargs):
            calls.append(kwargs)
            return {
                "id": "resp_synthesized",
                "object": "response",
                "status": "completed",
                "output_text": "done",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            }

        await hooks._external_web_search_synthesize_or_fallback(
            request_kwargs={"model": "legacy-chat", "input": "Use web_search."},
            search_results=(
                "Web search results for query: sample subject factor A\n"
                "Title: Example source\n"
                "URL: https://example.test/source-two\n"
                "Snippet: useful snippet.\n\n"
                "Retrieved page content for URL: https://example.test/source-two\n"
                f"\n{long_page}"
            ),
            queries=["sample subject factor A"],
            source_urls=["https://example.test/source-two"],
            original_function=original_function,
        )

        self.assertEqual(len(calls), 1)
        input_text = calls[0].get("input", "")
        self.assertIn("https://example.test/source-two", input_text)
        self.assertIn("Evidence section trimmed for synthesis", input_text)
        self.assertLess(len(input_text), 4000)
        self.assertNotIn("A" * 3000, input_text)
        metadata_evidence = calls[0].get("litellm_metadata", {}).get(
            "external_web_search_search_results",
            "",
        )
        self.assertIn("Evidence section trimmed for synthesis", metadata_evidence)
        self.assertLess(len(metadata_evidence), 2500)

    async def test_responses_chat_external_web_search_bridge_initial_call_forces_low_reasoning(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {
                "id": "resp_no_tool",
                "object": "response",
                "status": "completed",
                "output_text": "done",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            }

        await hooks._execute_responses_chat_bridge_call(
            original_generic_function,
            {
                "model": "openai/vendor-chat",
                "input": "Use web_search.",
                "reasoning": {"effort": "xhigh", "summary": "auto"},
                "extra_body": {"reasoning": {"effort": "xhigh"}},
                "litellm_metadata": {
                    hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY: True,
                    hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                },
                "tools": [
                    {
                        "type": "function",
                        "name": "web_search",
                        "parameters": {"type": "object"},
                    }
                ],
            },
            original_request_kwargs={
                "model": "legacy-chat",
                "input": "Use web_search.",
                "reasoning": {"effort": "xhigh", "summary": "auto"},
            },
            start_event="responses_chat_bridge_preemptive_start",
            error_event="responses_chat_bridge_preemptive_error",
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("reasoning"), {"effort": "low"})
        self.assertEqual(calls[0].get("extra_body", {}).get("reasoning"), {"effort": "low"})

    def test_external_web_search_continuation_leaves_next_action_to_model(self) -> None:
        hooks, _ = load_hook_module()

        kwargs = hooks._external_web_search_continuation_kwargs(
            {
                "model": "openai/vendor-chat",
                "input": "深挖调查这个说法是否成立。",
                "reasoning": {"effort": "xhigh"},
                "extra_body": {"reasoning": {"effort": "xhigh"}},
                "litellm_params": {"reasoning_effort": "xhigh"},
            },
            search_results=(
                "Web search results for query: verify claim\n"
                "Title: Candidate source\n"
                "URL: https://example.test/source\n"
                "Snippet: Search-result snippet only."
            ),
            queries=["verify claim"],
            completed_actions=[{"type": "search", "query": "verify claim"}],
            round_number=1,
        )

        self.assertNotIn("max_output_tokens", kwargs)
        self.assertEqual(kwargs.get("reasoning"), {"effort": "xhigh"})
        self.assertEqual(
            kwargs.get("extra_body", {}).get("reasoning"),
            {"effort": "xhigh"},
        )
        self.assertEqual(
            kwargs.get("litellm_params", {}).get("reasoning_effort"),
            "xhigh",
        )
        self.assertNotIn("tool_choice", kwargs)
        self.assertIn("Decide the next step now", kwargs.get("input", ""))
        self.assertIn("https://example.test/source", kwargs.get("input", ""))

    async def test_external_web_search_continuation_retries_transient_model_429(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class RateLimitError(Exception):
            status_code = 429

        async def original_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RateLimitError("upstream 429")
            return {
                "id": "resp_continued",
                "object": "response",
                "status": "completed",
                "output_text": "Sample subject evidence summary.",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Sample subject evidence summary.",
                            }
                        ],
                    }
                ],
            }

        self.set_env("LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRIES", "1")
        self.set_env("LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS", "0")

        response = await hooks._external_web_search_continue_or_synthesize(
            request_kwargs={
                "model": "balanced-chat",
                "input": "Use web_search.",
                "model_info": {
                    "id": "79f0dc70",
                    "route_key": "provider_chat / openai/vendor-chat / key=default",
                },
            },
            search_results=(
                "Web search results for query: sample subject Signal\n"
                "Title: Source One\n"
                "URL: https://example.test/one\n"
                "Snippet: useful snippet."
            ),
            queries=["sample subject Signal"],
            source_urls=["https://example.test/one"],
            round_number=1,
            original_function=original_function,
        )

        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["stream"])
        self.assertTrue(calls[1]["stream"])
        self.assertEqual(response.get("output_text"), "Sample subject evidence summary.")

    async def test_external_web_search_empty_continuation_synthesizes_without_route_recovery(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_function(**kwargs):
            calls.append(kwargs)
            metadata = kwargs.get("litellm_metadata", {})
            if metadata.get("external_web_search_continuation"):
                return {
                    "id": "resp_empty_continuation",
                    "object": "response",
                    "status": "completed",
                    "output_text": "",
                    "output": [],
                }
            if metadata.get("external_web_search_synthesis"):
                return {
                    "id": "resp_synthesized_after_empty_continuation",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Synthesized answer from evidence. https://example.test/one",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Synthesized answer from evidence. https://example.test/one",
                                }
                            ],
                        }
                    ],
                }
            self.fail("unexpected original_function call")

        response = await hooks._external_web_search_continue_or_synthesize(
            request_kwargs={
                "model": "legacy-chat",
                "input": "Use web_search.",
                "stream": True,
            },
            search_results=(
                "Web search results for query: sample subject Signal\n"
                "Title: Source One\n"
                "URL: https://example.test/one\n"
                "Snippet: useful snippet."
            ),
            queries=["sample subject Signal"],
            source_urls=["https://example.test/one"],
            round_number=2,
            original_function=original_function,
        )

        self.assertEqual(response.get("output_text"), "Synthesized answer from evidence. https://example.test/one")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["litellm_metadata"].get("external_web_search_continuation"))
        self.assertTrue(calls[1]["litellm_metadata"].get("external_web_search_synthesis"))

    def test_external_web_search_continuation_keeps_client_tools_for_curl_or_browser(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "model": "openai/vendor-chat",
            "input": "Use web_search and inspect example index.",
            "tools": [
                {"type": "web_search"},
                {
                    "type": "namespace",
                    "name": "functions",
                    "tools": [
                        {
                            "type": "function",
                            "name": "exec_command",
                            "description": "Run shell commands such as curl.",
                            "parameters": {
                                "type": "object",
                                "properties": {"cmd": {"type": "string"}},
                            },
                        },
                        {
                            "type": "function",
                            "name": "browser_open",
                            "description": "Open a browser URL.",
                            "parameters": {
                                "type": "object",
                                "properties": {"url": {"type": "string"}},
                            },
                        },
                    ],
                },
            ],
        }

        continuation_kwargs = hooks._external_web_search_continuation_kwargs(
            request_kwargs,
            search_results=(
                "Web search results for query: sample subject example index\n"
                "Title: example index source\n"
                "URL: https://example.test/source-index\n"
                "Snippet: Example evidence."
            ),
            queries=["sample subject example index"],
            completed_actions=[{"type": "search", "query": "sample subject example index"}],
            round_number=1,
        )

        tool_types = [tool.get("type") for tool in continuation_kwargs.get("tools", [])]
        tool_names = [tool.get("name") for tool in continuation_kwargs.get("tools", [])]
        self.assertNotIn("web_search", tool_types)
        self.assertIn("namespace", tool_types)
        self.assertIn("web_search", tool_names)
        self.assertIn("Decide the next step", continuation_kwargs.get("instructions", ""))

        chat_payload = hooks._external_web_search_chat_tool_payload(
            continuation_kwargs,
            continuation_kwargs,
        )
        chat_tool_names = [
            tool.get("name") or tool.get("function", {}).get("name")
            for tool in chat_payload.get("tools", [])
        ]
        self.assertIn("exec_command", chat_tool_names)
        self.assertIn("browser_open", chat_tool_names)
        self.assertIn("web_search", chat_tool_names)

    async def test_external_web_search_stream_empty_continuation_does_not_route_recover(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll
        route_recovery_calls = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: sample subject transporter\n"
                "Title: Transporter source\n"
                "URL: https://example.test/source\n"
                "Snippet: Transporter evidence.",
                ["https://example.test/source"],
                action,
            )

        async def fake_route_recovery_poll(request_data, exception):
            route_recovery_calls.append((request_data, exception))
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_unexpected_recovery",
                    "object": "response",
                    "status": "completed",
                    "output_text": "unexpected recovery",
                    "output": [],
                },
            }

        calls = []

        async def original_function(**kwargs):
            calls.append(kwargs)
            metadata = kwargs.get("litellm_metadata", {})
            if metadata.get("external_web_search_continuation"):
                return {
                    "id": "resp_empty_continuation",
                    "object": "response",
                    "status": "completed",
                    "output_text": "",
                    "output": [],
                }
            if metadata.get("external_web_search_synthesis"):
                return {
                    "id": "resp_final",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Final answer. https://example.test/source",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Final answer. https://example.test/source",
                                }
                            ],
                        }
                    ],
                }
            self.fail("unexpected original_function call")

        hooks._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "sample subject transporter"}),
                    "status": "completed",
                }
            ],
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._resolve_web_search_function_calls_stream_rounds(
                response,
                {
                    "model": "openai/vendor-chat",
                    "input": "Use web_search for sample subject transporter evidence.",
                    "tools": [{"type": "web_search"}],
                    "stream": True,
                },
                original_function=original_function,
            )
        ]

        self.assertEqual(route_recovery_calls, [])
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertIn("Final answer", json.dumps(chunks[-1], ensure_ascii=False))
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["litellm_metadata"].get("external_web_search_continuation"))
        self.assertTrue(calls[1]["litellm_metadata"].get("external_web_search_synthesis"))

    async def test_external_web_search_continuation_structured_message_stops(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: sample marker context\n"
                "Title: Source\n"
                "URL: https://example.test/source\n"
                "Snippet: Sample evidence.",
                ["https://example.test/source"],
                action,
            )

        calls = []

        async def original_function(**kwargs):
            calls.append(kwargs)
            metadata = kwargs.get("litellm_metadata", {})
            if metadata.get("external_web_search_continuation"):
                return {
                    "id": "resp_continuation_preamble",
                    "object": "response",
                    "status": "completed",
                    "output_text": "我来获取一下sample marker 的公开背景，并检查来源。",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "我来获取一下sample marker 的公开背景，并检查来源。",
                                }
                            ],
                        }
                    ],
                }
            if metadata.get("external_web_search_synthesis"):
                self.fail("structured completed assistant message should not synthesize")
            self.fail("unexpected original_function call")

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "sample marker context"}),
                    "status": "completed",
                }
            ],
        }

        resolved = await hooks._resolve_web_search_function_calls(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "sample marker context answer.",
                "tools": [{"type": "web_search"}],
            },
            original_function=original_function,
        )

        self.assertEqual(resolved["output_text"], "我来获取一下sample marker 的公开背景，并检查来源。")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["litellm_metadata"].get("external_web_search_continuation"))
        self.assertNotIn("external_web_search_synthesis", calls[0]["litellm_metadata"])

    async def test_external_web_search_continuation_raises_after_5xx_without_dumping_results(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class ServiceUnavailable(Exception):
            status_code = 503

        async def original_function(**kwargs):
            calls.append(kwargs)
            raise ServiceUnavailable("upstream 503")

        self.set_env("LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRIES", "2")
        self.set_env("LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS", "0")

        with self.assertRaises(ServiceUnavailable) as context:
            await hooks._external_web_search_continue_or_synthesize(
                request_kwargs={
                    "model": "balanced-chat",
                    "input": "Use web_search.",
                },
                search_results=(
                    "Web search results for query: sample subject Signal\n"
                    "Title: Source One\n"
                    "URL: https://example.test/one\n"
                    "Snippet: useful snippet."
                ),
                queries=["sample subject Signal"],
                source_urls=["https://example.test/one"],
                round_number=1,
                original_function=original_function,
            )

        self.assertEqual(len(calls), 1)
        recovery_request = hooks._external_web_search_recovery_request_from_exception(
            context.exception
        )
        self.assertIsInstance(recovery_request, dict)
        self.assertTrue(
            recovery_request["litellm_metadata"]["external_web_search_continuation"]
        )
        self.assertNotIn(
            "external_web_search_synthesis",
            recovery_request["litellm_metadata"],
        )
        self.assertTrue(recovery_request["stream"])
        text = str(context.exception)
        self.assertNotIn("Source One", text)
        self.assertNotIn("https://example.test/one", text)

    async def test_external_web_search_continuation_504_timeout_keeps_continuation_recovery(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class GatewayTimeout(Exception):
            status_code = "504"
            body = {
                "error": {
                    "message": "所有渠道在 60s 内均失败",
                    "type": "bad_response_status_code",
                }
            }

            def __setattr__(self, name, value):
                if name == "external_web_search_recovery_request":
                    raise AttributeError(name)
                return super().__setattr__(name, value)

        async def original_function(**kwargs):
            calls.append(kwargs)
            raise GatewayTimeout("upstream gateway timeout")

        with self.assertRaises(GatewayTimeout) as context:
            await hooks._external_web_search_continue_or_synthesize(
                request_kwargs={
                    "model": "legacy-chat",
                    "input": "Use web_search.",
                    "stream": True,
                },
                search_results=(
                    "Page text matches for pattern: Latest Python 3 Release\n"
                    "URL: https://www.python.org/downloads/\n"
                    "No readable matches for pattern."
                ),
                queries=["Latest Python 3 Release in https://www.python.org/downloads/"],
                source_urls=["https://www.python.org/downloads/"],
                round_number=1,
                original_function=original_function,
            )

        self.assertEqual(len(calls), 1)
        recovery_request = hooks._external_web_search_recovery_request_from_exception(
            context.exception
        )
        self.assertIsInstance(recovery_request, dict)
        recovery_metadata = recovery_request["litellm_metadata"]
        self.assertTrue(recovery_metadata.get("external_web_search_continuation"))
        self.assertNotIn("external_web_search_synthesis", recovery_metadata)
        self.assertTrue(recovery_request["stream"])
        self.assertEqual(
            [tool.get("name") for tool in recovery_request["tools"]],
            ["web_search", "fetch_content"],
        )

    async def test_external_web_search_continuation_async_stream_timeout_keeps_continuation_recovery(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def failing_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_continuation",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            raise RuntimeError("stream idle timeout")

        async def original_function(**kwargs):
            calls.append(kwargs)
            return failing_stream()

        with self.assertRaises(RuntimeError) as context:
            await hooks._external_web_search_continue_or_synthesize(
                request_kwargs={
                    "model": "balanced-chat",
                    "input": "Use web_search.",
                },
                search_results=(
                    "Web search results for query: sample subject Signal\n"
                    "Title: Source One\n"
                    "URL: https://example.test/one\n"
                    "Snippet: useful snippet."
                ),
                queries=["sample subject Signal"],
                source_urls=["https://example.test/one"],
                round_number=1,
                original_function=original_function,
            )

        self.assertEqual(len(calls), 1)
        recovery_request = hooks._external_web_search_recovery_request_from_exception(
            context.exception
        )
        self.assertIsInstance(recovery_request, dict)
        recovery_metadata = recovery_request.get("litellm_metadata", {})
        self.assertTrue(recovery_metadata.get("external_web_search_continuation"))
        self.assertNotIn("external_web_search_synthesis", recovery_metadata)
        self.assertTrue(recovery_request["stream"])
        text = str(context.exception)
        self.assertNotIn("Source One", text)
        self.assertNotIn("https://example.test/one", text)

    async def test_external_web_search_internal_generic_helper_skips_final_route_retry(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        calls = []

        class Router:
            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                return await original_generic_function(**kwargs)

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        class ServiceUnavailable(Exception):
            status_code = 503

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            raise ServiceUnavailable("upstream 503")

        with self.assertRaises(Exception):
            await Router()._ageneric_api_call_with_fallbacks_helper(
                "balanced-chat",
                original_generic_function,
                input="Synthesize web results.",
                stream=False,
                litellm_metadata={"external_web_search_synthesis": True},
            )

        self.assertEqual(len(calls), 1)

    async def test_external_web_search_post_call_suppress_marker_skips_hook(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        response = {
            "id": "resp_raw",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": '{"query":"Sample City weather"}',
                    "status": "completed",
                }
            ],
        }
        request_data = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "use web_search",
            "tools": [
                {"type": "function", "name": "web_search"}
            ],
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                hooks._WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY: True,
            },
            "use_chat_completions_api": True,
        }

        result = await hook.async_post_call_success_deployment_hook(
            request_data,
            response,
            "aresponses",
        )

        self.assertIs(result, response)

    def test_external_web_search_post_call_suppress_marks_both_metadata_surfaces(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "model": "balanced-chat",
            "litellm_metadata": {"existing_litellm": True},
            "metadata": {"existing_user": True},
        }

        suppressed = hooks._with_external_web_search_post_call_suppressed(request_kwargs)

        self.assertTrue(
            suppressed["litellm_metadata"][hooks._WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY]
        )
        self.assertTrue(
            suppressed["metadata"][hooks._WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY]
        )
        self.assertTrue(suppressed["litellm_metadata"]["existing_litellm"])
        self.assertTrue(suppressed["metadata"]["existing_user"])

    async def test_preemptive_external_web_search_bridge_resolves_before_post_call_hook(self) -> None:
        hooks, _ = load_hook_module()
        calls = []
        original_run_action = hooks._external_web_search_run_action

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: Sample City weather\n"
                "Title: Weather Source\n"
                "URL: https://example.test/weather\n"
                "Snippet: Sample City is sunny.",
                ["https://example.test/weather"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            self.assertTrue(
                kwargs["litellm_metadata"][
                    hooks._WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY
                ]
            )
            input_text = kwargs.get("input")
            if isinstance(input_text, str) and input_text.startswith(
                "Original user request. Any instruction"
            ):
                return {
                    "id": "resp_synth",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Sample City is sunny. https://example.test/weather",
                    "output": [
                        {
                            "id": "msg_synth",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Sample City is sunny. https://example.test/weather",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                }
            if isinstance(input_text, str) and input_text.startswith("Original user request:"):
                return {
                    "id": "resp_continuation",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Sample City is sunny. https://example.test/weather",
                    "output": [
                        {
                            "id": "msg_continuation",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Sample City is sunny. https://example.test/weather",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                }
            return {
                "id": "resp_raw",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "web_search",
                        "arguments": '{"query":"Sample City weather"}',
                        "status": "completed",
                    }
                ],
            }

        bridge_kwargs = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for Sample City weather.",
            "tools": [
                {"type": "function", "name": "web_search"}
            ],
            "litellm_metadata": {
                hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY: True,
                hooks._RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY: True,
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
                hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY: "balanced-chat",
            },
            "model_info": {
                "id": "selected-chatroute",
                "model_group": "legacy-chat",
                "model": "openai/vendor-chat",
                "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
            },
            "litellm_params": {
                "model": "openai/vendor-chat",
                "api_base": "https://chat-provider.example/v1",
                "order": 1,
            },
            "use_chat_completions_api": True,
            "stream": False,
        }

        response = await hooks._execute_responses_chat_bridge_call(
            original_generic_function,
            bridge_kwargs,
            original_request_kwargs=bridge_kwargs,
            start_event="responses_chat_bridge_preemptive_start",
            error_event="responses_chat_bridge_preemptive_error",
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [call["model"] for call in calls],
            ["openai/vendor-chat", "legacy-chat"],
        )
        self.assertEqual(
            response.get("output_text"),
            "Sample City is sunny. https://example.test/weather",
        )
        output_types = [
            item.get("type")
            for item in response.get("output", [])
            if isinstance(item, dict)
        ]
        self.assertIn("web_search_call", output_types)
        self.assertIn("message", output_types)

    async def test_external_web_search_bridge_executes_page_read_then_find_without_visible_pseudo_actions(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action
        executed_actions = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            executed_actions.append(action.copy())
            if action.get("type") == "openPage":
                return (
                    "Retrieved page content for URL: https://example.test/article\n\n"
                    "This page mentions factor A and example transporter transport.",
                    ["https://example.test/article"],
                    action,
                )
            if action.get("type") == "findInPage":
                return (
                    "Page text matches for pattern: factor A\n"
                    "URL: https://example.test/article\n\n"
                    "- This page mentions factor A and example transporter transport.",
                    ["https://example.test/article"],
                    action,
                )
            return (
                "Web search results for query: factor A source\n"
                "Title: Source\n"
                "URL: https://example.test/article\n"
                "Snippet: factor A source.",
                ["https://example.test/article"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        calls = []

        async def original_function(**kwargs):
            calls.append(kwargs)
            input_text = kwargs.get("input")
            if isinstance(input_text, str) and (
                input_text.startswith("Original user request:")
                or input_text.startswith("Original user request. Any instruction")
            ):
                if input_text.startswith("Original user request:") and "Page text matches for pattern" not in input_text:
                    return {
                        "id": "resp_find",
                        "object": "response",
                        "status": "completed",
                        "output": [
                            {
                                "type": "function_call",
                                "name": "web_search",
                                "arguments": json.dumps(
                                    {
                                        "url": "https://example.test/article",
                                        "pattern": "factor A",
                                    }
                                ),
                                "status": "completed",
                            }
                        ],
                    }
                return {
                    "id": "resp_final",
                    "object": "response",
                    "status": "completed",
                    "output_text": "factor A evidence found. https://example.test/article",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "factor A evidence found. https://example.test/article",
                                }
                            ],
                        }
                    ],
                }
            raise AssertionError("unexpected original_function input")

        response = {
            "id": "resp_read",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"url": "https://example.test/article"}),
                    "status": "completed",
                }
            ],
        }

        resolved = await hooks._resolve_web_search_function_calls(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "Use web_search and inspect the source page for factor A.",
                "tools": [{"type": "web_search"}],
            },
            original_function=original_function,
        )

        self.assertEqual(
            executed_actions,
            [
                {"type": "openPage", "url": "https://example.test/article"},
                {
                    "type": "findInPage",
                    "url": "https://example.test/article",
                    "pattern": "factor A",
                },
            ],
        )
        self.assertGreaterEqual(len(calls), 2)
        dumped = json.dumps(resolved)
        self.assertIn("factor A evidence found", dumped)
        self.assertIn('"type": "web_search_call"', dumped)
        self.assertNotIn('"type": "openPage"', dumped)
        self.assertNotIn('"type": "findInPage"', dumped)
        self.assertIn('"type": "search"', dumped)
        self.assertNotIn("bridge_action", dumped)

    async def test_external_web_search_model_decides_final_after_explicit_actions(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action
        executed_actions = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            executed_actions.append(action.copy())
            if action.get("type") == "openPage":
                return (
                    "Retrieved page content for URL: https://example.test/source\n\n"
                    "Source text mentions sample subject and example transporters.",
                    ["https://example.test/source"],
                    action,
                )
            if action.get("type") == "findInPage":
                return (
                    "Page text matches for pattern: sample subject\n"
                    "URL: https://example.test/source\n\n"
                    "- Source text mentions sample subject and example transporters.",
                    ["https://example.test/source"],
                    action,
                )
            return (
                "Web search results for query: sample subject factor A factor B\n"
                "Title: Candidate source\n"
                "URL: https://example.test/source\n"
                "Snippet: Candidate source snippet.",
                ["https://example.test/source"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        calls = []

        async def original_function(**kwargs):
            calls.append(kwargs)
            metadata = kwargs.get("litellm_metadata", {})
            self.assertTrue(metadata.get("external_web_search_continuation"))
            self.assertNotIn("external_web_search_synthesis", metadata)
            input_text = kwargs.get("input")
            if isinstance(input_text, str) and input_text.startswith("Original user request:"):
                self.assertIn("Retrieved page content for URL: https://example.test/source", input_text)
                self.assertIn("Page text matches for pattern", input_text)
                return {
                    "id": "resp_final_after_find",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Final answer after finding source text.",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Final answer after finding source text.",
                                }
                            ],
                        }
                    ],
                }
            raise AssertionError("unexpected continuation input after source actions")

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "sample subject factor A factor B"}),
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"url": "https://example.test/source"}),
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps(
                        {
                            "url": "https://example.test/source",
                            "pattern": "sample subject",
                        }
                    ),
                    "status": "completed",
                },
            ],
        }

        resolved = await hooks._resolve_web_search_function_calls(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "深挖调查sample subject是否能抑制factor-a factor-b（不使用skill）",
                "reasoning": {"effort": "xhigh"},
                "tools": [{"type": "web_search"}],
            },
            original_function=original_function,
        )

        self.assertEqual(
            executed_actions[:2],
            [
                {"type": "search", "query": "sample subject factor A factor B"},
                {"type": "openPage", "url": "https://example.test/source"},
            ],
        )
        self.assertEqual(
            [
                action.get("pattern")
                for action in executed_actions
                if action.get("type") == "findInPage"
            ],
            ["sample subject"],
        )
        self.assertEqual(len(calls), 1)
        self.assertNotIn("tool_choice", calls[0])
        self.assertEqual(calls[0].get("reasoning"), {"effort": "xhigh"})
        self.assertIn("Final answer after finding source text", json.dumps(resolved))

    async def test_external_web_search_model_chooses_next_result_page(self) -> None:
        hooks, _ = load_hook_module()
        original_run_actions = hooks._external_web_search_run_actions
        executed_actions = []
        continuation_calls = []

        async def fake_run_actions(actions, page_cache, page_fetch_tasks, request_kwargs=None):
            executed_actions.extend(action.copy() for action in actions)
            action = actions[0]
            page = action.get("page", "1")
            if page == "1":
                return (
                    "Web search results for query: sample city weather\n\n"
                    "Title: First result\n"
                    "URL: https://example.test/weather/one\n"
                    "Snippet: First page weather result.",
                    ["https://example.test/weather/one"],
                    [["https://example.test/weather/one"]],
                    actions,
                )
            self.assertEqual(page, "2")
            return (
                "Web search results for query: sample city weather\n"
                "Result page: 2\n\n"
                "Title: Second-page result\n"
                "URL: https://example.test/weather/two\n"
                "Snippet: The requested forecast is here.",
                ["https://example.test/weather/two"],
                [["https://example.test/weather/two"]],
                actions,
            )

        hooks._external_web_search_run_actions = fake_run_actions
        self.addCleanup(
            setattr,
            hooks,
            "_external_web_search_run_actions",
            original_run_actions,
        )

        async def original_function(**kwargs):
            continuation_calls.append(kwargs)
            if len(continuation_calls) == 1:
                self.assertIn("First page weather result.", kwargs.get("input", ""))
                return {
                    "id": "resp_page_two",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "web_search",
                            "arguments": json.dumps(
                                {"query": "sample city weather", "page": 2}
                            ),
                            "status": "completed",
                        }
                    ],
                }
            self.assertIn("Result page: 2", kwargs.get("input", ""))
            return {
                "id": "resp_page_final",
                "object": "response",
                "status": "completed",
                "output_text": "The second result page has the forecast.",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "The second result page has the forecast.",
                            }
                        ],
                    }
                ],
            }

        response = {
            "id": "resp_page_one",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "sample city weather"}),
                    "status": "completed",
                }
            ],
        }

        resolved = await hooks._resolve_web_search_function_calls(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "Use web_search for the sample city weather.",
                "tools": [{"type": "web_search"}],
            },
            original_function=original_function,
        )

        self.assertEqual(
            executed_actions,
            [
                {"type": "search", "query": "sample city weather"},
                {"type": "search", "query": "sample city weather", "page": "2"},
            ],
        )
        self.assertEqual(len(continuation_calls), 2)
        self.assertIn("second result page", json.dumps(resolved))

    async def test_external_web_search_stream_explicit_source_read_before_continuation(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action
        executed_actions = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            executed_actions.append(action.copy())
            if action.get("type") == "openPage":
                return (
                    "Retrieved page content for URL: https://example.test/source\n\n"
                    "Primary source text mentions sample subject, factor A, and factor B.",
                    ["https://example.test/source"],
                    action,
                )
            if action.get("type") == "findInPage":
                return (
                    f"Page text matches for pattern: {action.get('pattern')}\n"
                    "URL: https://example.test/source\n\n"
                    "- Matching source line.",
                    ["https://example.test/source"],
                    action,
                )
            return (
                "Web search results for query: sample subject factor A factor B\n"
                "Title: Candidate source\n"
                "URL: https://example.test/source\n"
                "Snippet: Candidate source snippet.",
                ["https://example.test/source"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        calls = []

        async def original_function(**kwargs):
            calls.append(kwargs)
            input_text = kwargs.get("input")
            self.assertIsInstance(input_text, str)
            self.assertIn("Retrieved page content for URL: https://example.test/source", input_text)
            self.assertIn("Page text matches for pattern", input_text)
            return {
                "id": "resp_stream_final",
                "object": "response",
                "status": "completed",
                "output_text": "Final stream answer. https://example.test/source",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Final stream answer. https://example.test/source",
                            }
                        ],
                    }
                ],
            }

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "sample subject factor A factor B"}),
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"url": "https://example.test/source"}),
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps(
                        {
                            "url": "https://example.test/source",
                            "pattern": "factor A",
                        }
                    ),
                    "status": "completed",
                },
            ],
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._resolve_web_search_function_calls_stream_rounds(
                response,
                {
                    "model": "openai/vendor-chat",
                    "input": "Use web_search for sample subject factor A factor B.",
                    "tools": [{"type": "web_search"}],
                    "stream": True,
                },
                original_function=original_function,
            )
        ]

        self.assertEqual(executed_actions[0], {"type": "search", "query": "sample subject factor A factor B"})
        self.assertEqual(executed_actions[1], {"type": "openPage", "url": "https://example.test/source"})
        self.assertEqual(
            [
                action.get("pattern")
                for action in executed_actions
                if action.get("type") == "findInPage"
            ],
            ["factor A"],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(chunks[-1]["type"], "response.completed")
        output = chunks[-1]["response"]["output"]
        queries = [
            item.get("action", {}).get("query")
            for item in output
            if item.get("type") == "web_search_call"
        ]
        self.assertIn("https://example.test/source", queries)
        self.assertIn("factor A in https://example.test/source", queries)
        self.assertIn("Final stream answer", json.dumps(chunks[-1], ensure_ascii=False))

    async def test_external_web_search_stream_prepares_continuation_recovery_before_continuation_task(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action
        bridge_module = importlib.import_module("litellm_menu.responses_web_search_bridge")
        original_bridge_run_action = bridge_module._external_web_search_run_action
        original_continue = bridge_module._external_web_search_continue_or_synthesize
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        executed_actions = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            executed_actions.append(action.copy())
            if action.get("type") == "openPage":
                return (
                    "Retrieved page content for URL: https://example.test/source\n\n"
                    "Primary source text mentions sample subject, factor A, and factor B.",
                    ["https://example.test/source"],
                    action,
                )
            if action.get("type") == "findInPage":
                return (
                    f"Page text matches for pattern: {action.get('pattern')}\n"
                    "URL: https://example.test/source\n\n"
                    "- Matching source line.",
                    ["https://example.test/source"],
                    action,
                )
            return (
                "Web search results for query: sample subject factor A factor B\n"
                "Title: Candidate source\n"
                "URL: https://example.test/source\n"
                "Snippet: Candidate source snippet.",
                ["https://example.test/source"],
                action,
            )

        class BoundaryError(Exception):
            status_code = 503

        async def fake_continue_or_synthesize(**kwargs):
            pending = bridge_module._external_web_search_pending_recovery_request(
                kwargs.get("request_kwargs")
            )
            self.assertIsInstance(pending, dict)
            metadata = pending.get("litellm_metadata", {})
            self.assertTrue(metadata.get("external_web_search_continuation"))
            self.assertNotIn("external_web_search_synthesis", metadata)
            self.assertEqual(
                [tool.get("name") for tool in pending.get("tools", [])],
                ["web_search", "fetch_content"],
            )
            self.assertIn("Decide the next step now", pending.get("input", ""))
            self.assertIn("Retrieved page content for URL: https://example.test/source", pending.get("input", ""))
            raise BoundaryError("missing answer boundary before continuation start")

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(copy.deepcopy(request_data))
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_recovered_prepared_continuation",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Recovered final answer. https://example.test/source",
                    "output": [
                        {
                            "id": "msg_recovered_prepared_continuation",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Recovered final answer. https://example.test/source",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        hooks._external_web_search_run_action = fake_run_action
        bridge_module._external_web_search_run_action = fake_run_action
        bridge_module._external_web_search_continue_or_synthesize = fake_continue_or_synthesize
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            bridge_module,
            "_external_web_search_run_action",
            original_bridge_run_action,
        )
        self.addCleanup(
            setattr,
            bridge_module,
            "_external_web_search_continue_or_synthesize",
            original_continue,
        )
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "sample subject factor A factor B"}),
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"url": "https://example.test/source"}),
                    "status": "completed",
                },
            ],
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._resolve_web_search_function_calls_stream_rounds(
                response,
                {
                    "model": "openai/vendor-chat",
                    "input": "Use web_search for sample subject factor A factor B.",
                    "tools": [{"type": "web_search"}],
                    "stream": True,
                },
                original_function=None,
            )
        ]

        self.assertEqual(executed_actions[0], {"type": "search", "query": "sample subject factor A factor B"})
        self.assertEqual(executed_actions[1], {"type": "openPage", "url": "https://example.test/source"})
        self.assertEqual(len(recovery_requests), 1)
        recovery_metadata = recovery_requests[0].get("litellm_metadata", {})
        self.assertTrue(recovery_metadata.get("external_web_search_continuation"))
        self.assertNotIn("external_web_search_synthesis", recovery_metadata)
        self.assertIn("Decide the next step now", recovery_requests[0].get("input", ""))
        self.assertIn("Recovered final answer. https://example.test/source", json.dumps(chunks, ensure_ascii=False))

    async def test_external_web_search_stream_synthesis_timeout_recovers_without_bridge_fallback_text(self) -> None:
        hooks, proxy_server = load_hook_module()
        original_run_action = hooks._external_web_search_run_action
        executed_actions = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            executed_actions.append(action.copy())
            if action.get("type") == "openPage":
                return (
                    "Retrieved page content for URL: https://example.test/source\n\n"
                    "Primary source text mentions sample subject.",
                    ["https://example.test/source"],
                    action,
                )
            if action.get("type") == "findInPage":
                return (
                    f"Page text matches for pattern: {action.get('pattern')}\n"
                    "URL: https://example.test/source\n\n"
                    "- Matching source line.",
                    ["https://example.test/source"],
                    action,
                )
            return (
                "Web search results for query: sample subject factor A factor B\n"
                "Title: Candidate source\n"
                "URL: https://example.test/source\n"
                "Snippet: Candidate source snippet.",
                ["https://example.test/source"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        class SynthesisTimeout(Exception):
            status_code = 504

        async def original_function(**kwargs):
            self.assertIsInstance(kwargs.get("input"), str)
            self.assertIn("Retrieved page content for URL: https://example.test/source", kwargs["input"])
            metadata = kwargs.get("litellm_metadata", {})
            self.assertTrue(metadata.get("external_web_search_synthesis"))
            self.assertNotIn("external_web_search_continuation", metadata)
            exc = SynthesisTimeout("upstream timed out during synthesis")
            exc.failed_deployment_id = "chatroute"
            exc.failed_deployment_order = 1
            raise exc

        recovery_calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "Recovered final answer."}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-recovered-web-search",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Recovered final answer.",
                    "output": [
                        {
                            "id": "msg-recovered-web-search",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Recovered final answer.",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "chatroute"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "chatroute-pro"},
                    },
                ]

            async def aresponses(self, **payload):
                recovery_calls.append(payload)
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "sample subject factor A factor B"}),
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"url": "https://example.test/source"}),
                    "status": "completed",
                },
            ],
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._resolve_web_search_function_calls_stream_rounds(
                response,
                {
                    "model": "openai/vendor-chat",
                    "input": "深挖调查sample subject是否能抑制factor-a factor-b",
                    "tools": [{"type": "web_search"}],
                    "stream": True,
                    "model_info": {"id": "chatroute", "order": 1},
                },
                original_function=original_function,
            )
        ]

        self.assertEqual(executed_actions[0], {"type": "search", "query": "sample subject factor A factor B"})
        self.assertEqual(executed_actions[1], {"type": "openPage", "url": "https://example.test/source"})
        self.assertTrue(recovery_calls)
        recovery_metadata = recovery_calls[-1].get("litellm_metadata", {})
        self.assertTrue(recovery_metadata.get("external_web_search_synthesis"))
        self.assertNotIn("external_web_search_continuation", recovery_metadata)
        dumped = json.dumps(chunks[-1], ensure_ascii=False)
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertIn("Recovered final answer", dumped)
        self.assertNotIn("web_search compatibility bridge", dumped)
        self.assertNotIn("could not retrieve usable source results", dumped)

    async def test_external_web_search_failed_search_without_urls_leaves_retry_query_to_model(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action
        executed_actions = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            executed_actions.append(action.copy())
            return (
                "Web search results for query: failed query\n\n"
                "Search failed for query 'failed query': ConnectError",
                [],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        continuation_calls = []

        async def original_function(**kwargs):
            continuation_calls.append(kwargs)
            if kwargs.get("litellm_metadata", {}).get(
                "external_web_search_continuation"
            ):
                self.assertIn("Search failed for query", kwargs.get("input", ""))
                if "final investigation step" in kwargs.get("input", ""):
                    return {
                        "id": "resp_retry_final",
                        "object": "response",
                        "status": "completed",
                        "output_text": "The retry still found no source.",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "The retry still found no source.",
                                    }
                                ],
                            }
                        ],
                    }
                return {
                    "id": "resp_retry_query",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "web_search",
                            "arguments": json.dumps({"query": "retry query"}),
                            "status": "completed",
                        }
                    ],
                }
            return {
                "id": "resp_retry_final",
                "object": "response",
                "status": "completed",
                "output_text": "The retry still found no source.",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "The retry still found no source.",
                            }
                        ],
                    }
                ],
            }

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "failed query"}),
                    "status": "completed",
                }
            ],
        }

        self.set_env("LITELLM_MENU_WEB_SEARCH_MAX_ROUNDS", "2")
        result = await hooks._resolve_web_search_function_calls(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "深挖调查这个说法。",
                "tools": [{"type": "web_search"}],
            },
            original_function=original_function,
        )

        self.assertEqual(
            executed_actions,
            [
                {"type": "search", "query": "failed query"},
                {"type": "search", "query": "retry query"},
            ],
        )
        # The round budget no longer forces a proxy-side synthesis turn. The
        # model itself closes the investigation on the final round.
        self.assertEqual(len(continuation_calls), 2)
        self.assertFalse(
            continuation_calls[-1]
            .get("litellm_metadata", {})
            .get("external_web_search_synthesis")
        )
        self.assertIn(
            "final investigation step",
            continuation_calls[-1].get("input", ""),
        )
        self.assertTrue(result.get("output"))

    async def test_external_web_search_continuation_gets_dedicated_stream_budgets(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: %s\n\nTitle: t\nURL: https://example.test/a\nSnippet: s."
                % action.get("query"),
                ["https://example.test/a"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(
            setattr,
            hooks,
            "_external_web_search_run_action",
            original_run_action,
        )
        continuation_calls = []

        async def original_function(**kwargs):
            continuation_calls.append(kwargs)
            return {
                "id": "resp_cont",
                "object": "response",
                "status": "completed",
                "output_text": "Answer with source https://example.test/a.",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Answer with source https://example.test/a.",
                            }
                        ],
                    }
                ],
            }

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "first query"}),
                    "status": "completed",
                }
            ],
        }
        await hooks._resolve_web_search_function_calls(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "查一下这个说法。",
                "tools": [{"type": "web_search"}],
            },
            original_function=original_function,
        )
        self.assertEqual(len(continuation_calls), 1)
        call = continuation_calls[0]
        # The hidden continuation turn must not inherit the interactive
        # 120s stream budgets: a reasoning upstream digesting the evidence
        # legitimately stays silent far longer.
        self.assertEqual(
            call.get("stream_idle_timeout_seconds"),
            hooks._EXTERNAL_WEB_SEARCH_CONTINUATION_STREAM_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            call.get("stream_start_timeout_seconds"),
            hooks._EXTERNAL_WEB_SEARCH_CONTINUATION_STREAM_TIMEOUT_SECONDS,
        )

    async def test_external_web_search_query_retries_transient_rate_limit(self) -> None:
        hooks, _ = load_hook_module()
        original_search = hooks._pi_web_access_module._pi_web_access_search_sync
        original_sleep = hooks.asyncio.sleep
        calls = []
        sleeps = []

        def fake_search(query, page=1):
            calls.append(query)
            if len(calls) < 3:
                raise RuntimeError("search provider rate limited (429)")
            return "Results for %s. https://example.test/source" % query, {
                "sources": ["https://example.test/source"]
            }

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        hooks._pi_web_access_module._pi_web_access_search_sync = fake_search
        hooks.asyncio.sleep = fake_sleep
        self.addCleanup(
            setattr,
            hooks._pi_web_access_module,
            "_pi_web_access_search_sync",
            original_search,
        )
        self.addCleanup(setattr, hooks.asyncio, "sleep", original_sleep)

        text, urls = await hooks._external_web_search_run_query("probe query")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [2.0, 4.0])
        self.assertIn("Results for probe query", text)
        self.assertEqual(urls, ["https://example.test/source"])

    async def test_external_web_search_query_surfaces_non_rate_limit_failure_immediately(self) -> None:
        hooks, _ = load_hook_module()
        original_search = hooks._pi_web_access_module._pi_web_access_search_sync
        calls = []

        def fake_search(query, page=1):
            calls.append(query)
            raise ValueError("upstream search outage")

        hooks._pi_web_access_module._pi_web_access_search_sync = fake_search
        self.addCleanup(
            setattr,
            hooks._pi_web_access_module,
            "_pi_web_access_search_sync",
            original_search,
        )
        text, urls = await hooks._external_web_search_run_query("probe query")
        self.assertEqual(len(calls), 1)
        self.assertEqual(urls, [])
        self.assertIn("upstream search outage", text)

    async def test_external_web_search_trace_events_carry_activity_tag(self) -> None:
        """Local search executions must never look like kimi-k3 successes.

        The local pi-web-access execution records and the upstream model
        calls are tagged with an explicit activity field so the logs cannot
        misattribute one to the other.
        """
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: %s\n\nTitle: t\nURL: https://example.test/a\nSnippet: s."
                % action.get("query"),
                ["https://example.test/a"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(
            setattr,
            hooks,
            "_external_web_search_run_action",
            original_run_action,
        )
        captured = []
        original_trace = hooks._trace_module._route_trace
        hooks._trace_module._route_trace = lambda event, **fields: captured.append(
            (event, fields)
        )
        self.addCleanup(
            setattr,
            hooks._trace_module,
            "_route_trace",
            original_trace,
        )

        async def original_function(**kwargs):
            return {
                "id": "resp_cont",
                "object": "response",
                "status": "completed",
                "output_text": "Answer with source https://example.test/a.",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Answer with source https://example.test/a.",
                            }
                        ],
                    }
                ],
            }

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "first query"}),
                    "status": "completed",
                }
            ],
        }
        await hooks._resolve_web_search_function_calls(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "查一下这个说法。",
                "tools": [{"type": "web_search"}],
            },
            original_function=original_function,
        )
        activities = {
            event: fields.get("activity")
            for event, fields in captured
            if event in {
                "external_web_search_bridge_actions_executed",
                "external_web_search_bridge_continuation_start",
                "external_web_search_bridge_continuation_done",
            }
        }
        self.assertEqual(
            activities["external_web_search_bridge_actions_executed"],
            "local_pi_web_access",
        )
        self.assertEqual(
            activities["external_web_search_bridge_continuation_start"],
            "upstream_model",
        )
        self.assertEqual(
            activities["external_web_search_bridge_continuation_done"],
            "upstream_model",
        )
        self.assertEqual(
            hooks._external_web_search_upstream_host(
                {"api_base": "https://gateway.example.test/v1"}
            ),
            "gateway.example.test",
        )

    def test_external_web_search_stream_timeout_overrides_win_over_globals(self) -> None:
        hooks, _ = load_hook_module()
        streaming = hooks._streaming_module
        request = {
            "model": "openai/vendor-chat",
            "input": "x",
            "stream": True,
            "stream_idle_timeout_seconds": 300.0,
            "stream_start_timeout_seconds": 0,
        }
        self.assertEqual(
            streaming._resolved_stream_timeout_seconds(
                request,
                "stream_idle_timeout_seconds",
                hooks._stream_idle_timeout_seconds_for_request,
            ),
            300.0,
        )
        self.assertEqual(
            streaming._resolved_stream_timeout_seconds(
                request,
                "stream_start_timeout_seconds",
                hooks._stream_start_timeout_seconds_for_request,
            ),
            0.0,
        )
        bare = {"model": "openai/vendor-chat", "input": "x", "stream": True}
        self.assertEqual(
            streaming._resolved_stream_timeout_seconds(
                bare,
                "stream_idle_timeout_seconds",
                hooks._stream_idle_timeout_seconds_for_request,
            ),
            hooks._stream_idle_timeout_seconds_for_request(bare),
        )

    async def test_external_web_search_stream_failed_search_without_urls_leaves_retry_query_to_model(self) -> None:
        hooks, proxy_server = load_hook_module()
        original_run_action = hooks._external_web_search_run_action
        executed_actions = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            executed_actions.append(action.copy())
            if action.get("query") == "retry query":
                return (
                    "Web search results for query: retry query\n\n"
                    "Title: Retry source\n"
                    "URL: https://example.test/retry\n"
                    "Snippet: Recovered source.",
                    ["https://example.test/retry"],
                    action,
                )
            return (
                "Web search results for query: failed query\n\n"
                "Search failed for query 'failed query': ConnectError",
                [],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        continuation_calls = []

        async def original_function(**kwargs):
            continuation_calls.append(kwargs)
            if len(continuation_calls) == 1:
                return {
                    "id": "resp_retry_query",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "web_search",
                            "arguments": json.dumps({"query": "retry query"}),
                            "status": "completed",
                        }
                    ],
                }
            return {
                "id": "resp_recovered",
                "object": "response",
                "status": "completed",
                "output_text": "Recovered answer",
                "output": [
                    {
                        "id": "msg_recovered",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "Recovered answer"}
                        ],
                    }
                ],
            }

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "failed query"}),
                    "status": "completed",
                }
            ],
        }

        self.set_env("LITELLM_MENU_WEB_SEARCH_MAX_ROUNDS", "3")
        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._resolve_web_search_function_calls_stream_rounds(
                response,
                {
                    "model": "openai/vendor-chat",
                    "input": "深挖调查这个说法。",
                    "tools": [{"type": "web_search"}],
                    "stream": True,
                },
                original_function=original_function,
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"].get("output_text"), "Recovered answer")
        self.assertEqual(
            executed_actions,
            [
                {"type": "search", "query": "failed query"},
                {"type": "search", "query": "retry query"},
            ],
        )
        self.assertEqual(len(continuation_calls), 2)
        self.assertTrue(
            continuation_calls[0].get("litellm_metadata", {}).get(
                "external_web_search_continuation"
            )
        )
        dumped = json.dumps(chunks, ensure_ascii=False)
        self.assertNotIn("No usable source results", dumped)
        self.assertNotIn("available evidence is insufficient", dumped)

    async def test_external_web_search_simple_lookup_may_answer_from_search_results(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action
        executed_actions = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            executed_actions.append(action.copy())
            return (
                "Web search results for query: OpenAI homepage URL\n"
                "Title: OpenAI\n"
                "URL: https://openai.com/\n"
                "Snippet: Official homepage.",
                ["https://openai.com/"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        calls = []

        async def original_function(**kwargs):
            calls.append(kwargs)
            return {
                "id": "resp_simple_final",
                "object": "response",
                "status": "completed",
                "output_text": "The OpenAI homepage URL is https://openai.com/.",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "The OpenAI homepage URL is https://openai.com/.",
                            }
                        ],
                    }
                ],
            }

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "OpenAI homepage URL"}),
                    "status": "completed",
                }
            ],
        }

        resolved = await hooks._resolve_web_search_function_calls(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "Use web_search to find the OpenAI homepage URL.",
                "tools": [{"type": "web_search"}],
            },
            original_function=original_function,
        )

        self.assertEqual(executed_actions, [{"type": "search", "query": "OpenAI homepage URL"}])
        self.assertEqual(len(calls), 1)
        self.assertIn("https://openai.com/", json.dumps(resolved))

    async def test_external_web_search_run_actions_reads_page_and_finds_text(self) -> None:
        hooks, _ = load_hook_module()
        original_reader = hooks._pi_web_access_module._pi_web_access_page_excerpt

        def fake_reader(url, *, timeout, max_chars):
            self.assertEqual(url, "https://example.test/article")
            self.assertGreater(timeout, 0)
            self.assertGreater(max_chars, 0)
            return "Example Domain body with factor A marker."

        hooks._pi_web_access_module._pi_web_access_page_excerpt = fake_reader
        self.addCleanup(
            setattr,
            hooks._pi_web_access_module,
            "_pi_web_access_page_excerpt",
            original_reader,
        )

        message, urls, by_action, completed = await hooks._external_web_search_run_actions(
            [
                {"type": "openPage", "url": "https://example.test/article"},
                {
                    "type": "findInPage",
                    "url": "https://example.test/article",
                    "pattern": "factor A",
                },
            ],
            {},
            {},
        )

        self.assertEqual(
            completed,
            [
                {"type": "openPage", "url": "https://example.test/article"},
                {
                    "type": "findInPage",
                    "url": "https://example.test/article",
                    "pattern": "factor A",
                },
            ],
        )
        self.assertEqual(urls, ["https://example.test/article"])
        self.assertEqual(
            by_action,
            [["https://example.test/article"], ["https://example.test/article"]],
        )
        self.assertIn("Retrieved page content for URL: https://example.test/article", message)
        self.assertIn("Page text matches for pattern: factor A", message)
        self.assertIn("factor A marker", message)

    async def test_external_web_search_find_scans_full_bounded_page(self) -> None:
        hooks, _ = load_hook_module()
        original_reader = hooks._pi_web_access_module._pi_web_access_page_excerpt
        observed_limits = []

        def fake_reader(url, *, timeout, max_chars):
            self.assertEqual(url, "https://example.test/source.py")
            self.assertGreater(timeout, 0)
            observed_limits.append(max_chars)
            return "imports and helpers\n" + ("x" * 6000) + "\npage: int = 1"

        hooks._pi_web_access_module._pi_web_access_page_excerpt = fake_reader
        self.addCleanup(
            setattr,
            hooks._pi_web_access_module,
            "_pi_web_access_page_excerpt",
            original_reader,
        )

        message, _urls, _by_action, completed = await hooks._external_web_search_run_actions(
            [
                {
                    "type": "findInPage",
                    "url": "https://example.test/source.py",
                    "pattern": "page: int = 1",
                }
            ],
            {},
            {},
        )

        self.assertEqual(observed_limits, [12000])
        self.assertEqual(
            completed,
            [
                {
                    "type": "findInPage",
                    "url": "https://example.test/source.py",
                    "pattern": "page: int = 1",
                }
            ],
        )
        self.assertIn("page: int = 1", message)

    async def test_external_web_search_model_chooses_source_after_full_result_page(self) -> None:
        hooks, _ = load_hook_module()
        original_run_actions = hooks._external_web_search_run_actions
        executed_actions = []
        continuation_calls = []

        def result_for_action(action):
            executed_actions.append(action.copy())
            action_type = action.get("type")
            if action_type == "search":
                return (
                    "Web search results for query: sample subject factor A inhibition\n"
                    "Title: Unrelated result\n"
                    "URL: https://example.test/unrelated\n"
                    "Snippet: Not useful.\n\n"
                    "Title: Primary transporter source\n"
                    "URL: https://example.test/sample-subject-factor-a\n"
                    "Snippet: The claim needs source-page inspection.",
                    [
                        "https://example.test/unrelated",
                        "https://example.test/sample-subject-factor-a",
                    ],
                    [[
                        "https://example.test/unrelated",
                        "https://example.test/sample-subject-factor-a",
                    ]],
                    [action],
                )
            if action_type == "openPage":
                return (
                    "Source page read: https://example.test/sample-subject-factor-a\n"
                    "Excerpt: The full page discusses sample subject and factor A transporter assays.",
                    [action.get("url", "")],
                    [[action.get("url", "")]],
                    [action],
                )
            if action_type == "findInPage":
                return (
                    f"Find in page: {action.get('pattern', '')} in {action.get('url', '')}\n"
                    "Match: sample subject factor A inhibition was not directly established.",
                    [action.get("url", "")],
                    [[action.get("url", "")]],
                    [action],
                )
            raise AssertionError(f"unexpected action: {action}")

        async def fake_run_actions(actions, page_cache, page_fetch_tasks, request_kwargs=None):
            results = [result_for_action(action) for action in actions]
            sections = [section for section, _urls, _by_action, _completed in results]
            source_urls = []
            source_urls_by_action = []
            completed_actions = []
            for _section, urls, by_action, completed in results:
                source_urls_by_action.extend(by_action)
                completed_actions.extend(completed)
                for url in urls:
                    if url not in source_urls:
                        source_urls.append(url)
            return (
                "\n\n".join(section for section in sections if section.strip()),
                source_urls,
                source_urls_by_action,
                completed_actions,
            )

        hooks._external_web_search_run_actions = fake_run_actions
        self.addCleanup(setattr, hooks, "_external_web_search_run_actions", original_run_actions)

        async def original_function(**kwargs):
            continuation_calls.append(kwargs)
            metadata = kwargs.get("litellm_metadata", {})
            if metadata.get("external_web_search_continuation"):
                self.assertIn("https://example.test/unrelated", kwargs.get("input", ""))
                self.assertIn(
                    "https://example.test/sample-subject-factor-a",
                    kwargs.get("input", ""),
                )
                continuation_count = sum(
                    1
                    for call in continuation_calls
                    if call.get("litellm_metadata", {}).get(
                        "external_web_search_continuation"
                    )
                )
                if continuation_count > 1:
                    return {
                        "id": "resp_model_final",
                        "object": "response",
                        "status": "completed",
                        "output_text": "Final after source inspection. https://example.test/sample-subject-factor-a",
                        "output": [
                            {
                                "id": "msg_model_final",
                                "type": "message",
                                "status": "completed",
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "Final after source inspection. https://example.test/sample-subject-factor-a",
                                        "annotations": [],
                                    }
                                ],
                            }
                        ],
                    }
                return {
                    "id": "resp_model_selected_source",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "web_search",
                            "arguments": json.dumps(
                                {"url": "https://example.test/sample-subject-factor-a"}
                            ),
                            "status": "completed",
                        }
                    ],
                }
            self.fail("model should make the final decision after opening the source")

        response = {
            "id": "resp_source_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": json.dumps({"query": "sample subject factor A inhibition"}),
                    "status": "completed",
                }
            ],
        }

        resolved = await hooks._resolve_web_search_function_calls(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "Deep dive determine whether sample subject inhibits factor A; use web_search.",
                "tools": [{"type": "web_search"}],
            },
            original_function=original_function,
        )

        self.assertEqual(len(continuation_calls), 2)
        self.assertEqual(
            executed_actions,
            [
                {"type": "search", "query": "sample subject factor A inhibition"},
                {"type": "openPage", "url": "https://example.test/sample-subject-factor-a"},
            ],
        )
        self.assertIn("Final after source inspection", json.dumps(resolved))

    def test_external_web_search_walker_ignores_non_string_type_fields(self) -> None:
        hooks, _ = load_hook_module()
        response = {
            "output": [
                {
                    "type": ["message"],
                    "content": [
                        {
                            "type": ["output_text"],
                            "text": "not a tool call",
                        }
                    ],
                },
                {
                    "type": "function_call",
                    "name": "web_search",
                    "arguments": '{"query":"Sample City weather"}',
                },
            ]
        }

        calls = hooks._web_search_function_calls(response)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "web_search")
