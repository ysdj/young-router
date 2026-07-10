from __future__ import annotations

from hook_test_utils import *


class HookTraceLoggingTests(HookTestCase):
    def test_route_trace_enabled_reads_state_file_live(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "route-trace.enabled"
            self.set_env("LITELLM_ROUTE_TRACE_STATE_FILE", str(state_file))
            self.set_env("LITELLM_MENU_ROUTE_TRACE", None)

            self.assertFalse(hooks._route_trace_enabled())

            state_file.write_text("1\n", encoding="utf-8")
            self.assertTrue(hooks._route_trace_enabled())

            state_file.unlink()
            self.assertFalse(hooks._route_trace_enabled())

    def test_route_trace_falls_back_to_env_when_state_file_is_missing(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "route-trace.enabled"
            self.set_env("LITELLM_ROUTE_TRACE_STATE_FILE", str(state_file))
            self.set_env("LITELLM_MENU_ROUTE_TRACE", "debug")

            self.assertTrue(hooks._route_trace_enabled())

    def test_route_trace_state_file_overrides_stale_startup_env(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "route-trace.enabled"
            self.set_env("LITELLM_ROUTE_TRACE_STATE_FILE", str(state_file))
            self.set_env("LITELLM_MENU_ROUTE_TRACE", "1")

            state_file.write_text("on\n", encoding="utf-8")
            self.assertTrue(hooks._route_trace_enabled())

            state_file.write_text("0\ndisabled_at=2026-06-12T06:00:00Z\n", encoding="utf-8")
            self.assertFalse(hooks._route_trace_enabled())

    def test_trace_request_preview_scans_tail_of_long_responses_input(self) -> None:
        hooks, _ = load_hook_module()
        input_items = [
            {"role": "user", "content": f"old context item {index}"}
            for index in range(90)
        ]
        input_items.extend(
            [
                {"role": "assistant", "content": "assistant tail"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "real latest user request marker",
                        }
                    ],
                },
            ]
        )

        preview = hooks._trace_request_preview({"input": input_items})

        self.assertEqual(preview["message_count"], 92)
        self.assertEqual(preview["latest_user"], "real latest user request marker")
        self.assertFalse(preview["latest_user_truncated"])
        self.assertFalse(preview["preview_truncated"])
        self.assertEqual(preview["scan_direction"], "tail")
        self.assertIn("real latest user request marker", preview["preview"])
        self.assertNotIn("old context item 0", preview["preview"])

    def test_trace_request_preview_marks_truncated_text(self) -> None:
        hooks, _ = load_hook_module()
        text = "x" * (hooks._ROUTE_TRACE_PREVIEW_MAX_CHARS + 20)

        preview = hooks._trace_request_preview(
            {"input": [{"role": "user", "content": text}]}
        )

        self.assertEqual(len(preview["latest_user"]), hooks._ROUTE_TRACE_PREVIEW_MAX_CHARS)
        self.assertTrue(preview["latest_user_truncated"])
        self.assertTrue(preview["preview_truncated"])
        self.assertEqual(preview["preview_limit"], hooks._ROUTE_TRACE_PREVIEW_MAX_CHARS)

    def test_trace_request_preview_skips_internal_context_as_latest_user(self) -> None:
        hooks, _ = load_hook_module()

        preview = hooks._trace_request_preview(
            {
                "input": [
                    {"role": "user", "content": "actual user request marker"},
                    {
                        "role": "user",
                        "content": (
                            "Another language model started to solve this problem "
                            "and produced a summary of its thinking process."
                        ),
                    },
                ]
            }
        )

        self.assertEqual(preview["latest_user"], "actual user request marker")
        self.assertEqual(preview["latest_user_kind"], "user_request")
        self.assertEqual(preview["internal_context_block_count"], 1)

    def test_trace_request_summary_includes_interface_reasoning_and_tools(self) -> None:
        hooks, _ = load_hook_module()

        summary = hooks._trace_request_summary(
            {
                "model": "balanced-chat",
                "input": [{"role": "user", "content": "weather today"}],
                "stream": True,
                "reasoning": {"effort": "xhigh"},
                "text": {"verbosity": "low"},
                "tools": [
                    {"type": "web_search"},
                    {"type": "function", "function": {"name": "lookup_order"}},
                ],
                "tool_choice": {"type": "web_search"},
                "proxy_server_request": {
                    "url": "http://127.0.0.1:4000/v1/responses",
                    "method": "POST",
                },
                "model_info": {
                    "id": "vendor-chat",
                    "upstream_url_surface": "openai/responses",
                    "supports_responses_web_search": False,
                },
            }
        )

        self.assertEqual(summary["interface"]["client_surface"], "responses")
        self.assertEqual(summary["interface"]["effective_upstream_surface"], "responses")
        self.assertEqual(summary["interface"]["requested_endpoint"], "/v1/responses")
        self.assertTrue(summary["interface"]["stream"])
        self.assertEqual(summary["reasoning"]["effort"], "xhigh")
        self.assertEqual(summary["reasoning"]["text_verbosity"], "low")
        self.assertEqual(summary["tools"]["count"], 2)
        self.assertIn("web_search", summary["tools"]["types"])
        self.assertIn("lookup_order", summary["tools"]["names"])
        self.assertTrue(summary["tools"]["has_web_search_tool"])

    def test_trace_request_summary_identifies_standalone_image_generation(self) -> None:
        hooks, _ = load_hook_module()

        summary = hooks._trace_request_summary(
            {
                "model": "gpt-image-2",
                "prompt": "draw",
                "call_type": "aimage_generation",
                "proxy_server_request": {
                    "url": "http://127.0.0.1:4000/v1/images/generations",
                    "method": "POST",
                },
                "model_info": {
                    "id": "image-provider",
                    "upstream_url_surface": "openai/responses",
                },
            }
        )

        self.assertEqual(summary["interface"]["client_surface"], "image_generation")
        self.assertEqual(summary["interface"]["effective_upstream_surface"], "image_generation")
        self.assertEqual(
            summary["interface"]["requested_endpoint"],
            "/v1/images/generations",
        )

    def test_trace_request_summary_does_not_fabricate_unknown_provider_route(self) -> None:
        hooks, _ = load_hook_module()

        unselected = hooks._trace_request_summary(
            {
                "model": "balanced-chat",
                "input": "pre-selection request",
                "stream": True,
            }
        )
        selected = hooks._trace_request_summary(
            {
                "model": "balanced-chat",
                "input": "selected request",
                "stream": True,
                "litellm_params": {
                    "model": "openai/vendor-chat",
                    "api_base": "https://chat-provider.example/v1",
                },
                "model_info": {
                    "id": "79f0dc70",
                    "provider": "provider_chat",
                    "api_key_name": "default",
                },
            }
        )

        self.assertIsNone(unselected["route_key"])
        self.assertEqual(
            selected["route_key"],
            "model=balanced-chat / provider=provider_chat / upstream=openai/vendor-chat / host=chat-provider.example / key=default",
        )

    def test_trace_tool_call_summary_extracts_responses_and_chat_calls(self) -> None:
        hooks, _ = load_hook_module()

        summary = hooks._trace_tool_call_summary(
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "_litellm_web_search",
                        "call_id": "call_web",
                        "arguments": '{"query":"Sample City weather"}',
                    },
                    {
                        "type": "custom_tool_call",
                        "name": "custom_lookup",
                        "id": "call_custom",
                        "input": {"city": "Example City"},
                    },
                    {
                        "type": "web_search_call",
                        "id": "ws_1",
                        "action": {"type": "search", "query": "Sample City weather"},
                    },
                ],
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "chat_call",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_order",
                                        "arguments": '{"id":"A1"}',
                                    },
                                }
                            ]
                        }
                    }
                ],
            }
        )

        self.assertEqual(summary["count"], 4)
        self.assertIn("_litellm_web_search", summary["names"])
        self.assertIn("custom_lookup", summary["names"])
        self.assertIn("web_search", summary["names"])
        self.assertIn("lookup_order", summary["names"])
        self.assertIn("web_search_call", summary["types"])

    def test_trace_session_context_extracts_thread_id_and_name(self) -> None:
        hooks, _ = load_hook_module()

        session = hooks._trace_session_context(
            {
                "metadata": {
                    "codex_thread_id": "thread-123",
                    "codex_thread_title": "Route trace debug",
                },
                "model_info": {"id": "deployment-id-must-not-win"},
            }
        )

        self.assertEqual(session["id"], "thread-123")
        self.assertEqual(session["name"], "Route trace debug")
        self.assertEqual(session["id_key"], "codex_thread_id")

    async def test_recent_request_success_log_is_safe_summary(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
            end = start + timedelta(milliseconds=321)
            kwargs = {
                "call_type": "aresponses",
                "model": "default-chat",
                "messages": [{"role": "user", "content": "SECRET_PROMPT_BODY"}],
                "api_key": "sk-test-secret",
                "extra_headers": {"Authorization": "Bearer SECRET_AUTH_VALUE"},
                "litellm_call_id": "call-123",
                "tools": [{"type": "image_generation"}],
                "tool_choice": "auto",
                "litellm_params": {
                    "model": "openai/gpt-upstream",
                    "api_base": "https://provider.example/v1",
                    "metadata": {
                        "thread_id": "thread-abc",
                        "model_info": {
                            "id": "deployment-1",
                            "provider": "provider-a",
                            "order": 2,
                        },
                    },
                },
                "response_cost": 0.0123,
            }
            response = {"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}

            await hook.async_log_success_event(kwargs, response, start, end)

            raw = log_path.read_text(encoding="utf-8")
            record = json.loads(raw)
            self.assertEqual(record["status"], "success")
            self.assertEqual(record["duration_ms"], 321)
            self.assertEqual(record["model_group"], "default-chat")
            self.assertEqual(record["deployment_id"], "deployment-1")
            self.assertEqual(record["deployment_token"], "deployment-1")
            self.assertEqual(
                record["route_key"],
                "model=default-chat / provider=provider-a / upstream=openai/gpt-upstream / host=provider.example / order=2",
            )
            self.assertEqual(record["deployment_order"], 2)
            self.assertEqual(record["provider"], "provider-a")
            self.assertEqual(record["api_base_host"], "provider.example")
            self.assertEqual(record["request_id"], "call-123")
            self.assertEqual(record["usage"]["total_tokens"], 15)
            self.assertIn("image_generation", record["tool_types"])
            self.assertNotIn("SECRET_PROMPT_BODY", raw)
            self.assertNotIn("SECRETKEYVALUE", raw)
            self.assertNotIn("SECRET_AUTH_VALUE", raw)
            self.assertNotIn("Authorization", raw)

    async def test_recent_request_failure_log_omits_error_message_body(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        class ProviderError(Exception):
            status_code = 429

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
            end = start + timedelta(milliseconds=42)
            exc = ProviderError("SECRET_ERROR_BODY rate limit")
            kwargs = {
                "call_type": "acompletion",
                "model": "default-chat",
                "exception": exc,
                "messages": [{"role": "user", "content": "SECRET_PROMPT_BODY"}],
                "standard_logging_object": {
                    "error_type": "RateLimitError",
                    "error_status": "429",
                    "error_message": "SECRET_STANDARD_ERROR",
                },
            }

            await hook.async_log_failure_event(kwargs, None, start, end)

            raw = log_path.read_text(encoding="utf-8")
            record = json.loads(raw)
            self.assertEqual(record["status"], "failure")
            self.assertEqual(record["error"]["type"], "ProviderError")
            self.assertEqual(record["error"]["status_code"], 429)
            self.assertEqual(record["error"]["reason"], "upstream-status-429")
            self.assertNotIn("SECRET_ERROR_BODY", raw)
            self.assertNotIn("SECRET_STANDARD_ERROR", raw)
            self.assertNotIn("SECRET_PROMPT_BODY", raw)

    def test_recent_request_rotation_keeps_bounded_current_and_backup_tail(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            cap = hooks._RECENT_REQUESTS_MIN_MAX_BYTES
            log_path.write_text("a" * (cap + 1024), encoding="utf-8")
            self.set_log_env(log_path)
            self.set_env("LITELLM_MENU_LOG_MAX_BYTES", str(cap))

            hooks._append_recent_request({"status": "success", "marker": "latest"})

            self.assertLessEqual(log_path.stat().st_size, cap + 128)
            backup_path = Path(f"{log_path}.1")
            self.assertTrue(backup_path.exists())
            self.assertLessEqual(backup_path.stat().st_size, cap)
            self.assertEqual(log_path.read_text(encoding="utf-8")[:cap], "a" * cap)
            self.assertIn('"marker": "latest"', log_path.read_text(encoding="utf-8"))

    def test_recent_request_rotation_uses_local_log_cap_key(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env("LITELLM_MENU_LOG_MAX_BYTES", "300000")

        self.assertEqual(hooks._recent_requests_max_bytes(), 300000)

    async def test_deployment_failover_trace_records_route_key(self) -> None:
        hooks, _ = load_hook_module()

        class TemporaryFailure(Exception):
            status_code = 503

        self.set_env("LITELLM_MENU_ROUTE_TRACE", "1")
        self.set_env("LITELLM_ROUTE_TRACE_STATE_FILE", None)
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {
                "model": "openai/default-chat",
                "metadata": {
                    "model_info": {
                        "id": "openai-default-chat-provider_alpha-team-o1",
                        "route_key": "provider_alpha / openai/default-chat / key=team / order=1",
                        "provider": "provider_alpha",
                        "api_key_name": "team",
                        "order": 1,
                    },
                },
            },
        }
        error = TemporaryFailure("boom")

        with self.assertLogs("litellm_menu.route_trace", level="WARNING") as captured:
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        raw_payload = captured.output[0].split("litellm_route_trace ", 1)[1]
        record = json.loads(raw_payload)
        self.assertEqual(record["event"], "deployment_failover_marked")
        self.assertEqual(record["deployment_token"], "openai-default-chat-provider_alpha-team-o1")
        self.assertEqual(record["route_key"], "provider_alpha / openai/default-chat / key=team / order=1")
        self.assertEqual(
            record["exception"]["failed_deployment_route_key"],
            "provider_alpha / openai/default-chat / key=team / order=1",
        )


if __name__ == "__main__":
    unittest.main()
