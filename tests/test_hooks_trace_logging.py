from __future__ import annotations

from hook_test_utils import *


def _declared_stream_start_seconds() -> float:
    """The stream-start budget this deployment is configured to use.

    Read from the module that owns it rather than restated in an assertion:
    the default moved from 120 s to 300 s for a real reason (a max-effort turn
    sat longer than two minutes between upstream chunks and was cut), and a
    copied number turns that deliberate change into a test failure nobody can
    interpret.  What these tests are about is that the trace *reports* the
    deployed budget; which value the deployment chose is not their subject.
    """

    from young_router.proxy import routing as routing_module

    return routing_module._stream_start_timeout_seconds_for_request({})


class HookTraceLoggingTests(HookTestCase):
    def test_the_row_eviction_keeps_a_stream_that_is_still_reporting(self) -> None:
        """The cap evicts the least recently touched row, not the oldest one.

        Eviction pops the first entry, and re-inserting under an existing key
        does not move it, so a long stream kept its original position and could
        be dropped while it was still reporting — its settlement then found no
        entry and its row stayed "sending" in the viewer.
        """

        hooks, _ = load_hook_module()
        cap = hooks._PENDING_RECENT_REQUESTS_MAX
        with hooks._PENDING_RECENT_REQUESTS_LOCK:
            hooks._PENDING_RECENT_REQUESTS.clear()
        self.addCleanup(hooks._PENDING_RECENT_REQUESTS.clear)
        for index in range(cap):
            hooks._track_recent_request({
                "request_id": f"row-{index}",
                "status": "stream",
                "model": "default-chat",
            })
        # The oldest row is still alive: its heartbeat must protect it.
        hooks._touch_recent_request("row-0", interval_seconds=9999, )
        hooks._track_recent_request({"request_id": "row-new", "status": "stream", "model": "default-chat"})
        with hooks._PENDING_RECENT_REQUESTS_LOCK:
            self.assertIn("row-0", hooks._PENDING_RECENT_REQUESTS)
            self.assertNotIn("row-1", hooks._PENDING_RECENT_REQUESTS)

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
        # The summary states the budget the route step would deploy, not a
        # number copied into this test.  The cap moved from 120 s to 300 s
        # because a max-effort turn genuinely sat longer than two minutes
        # between upstream chunks, and a hardcoded 120 made that change look
        # like a regression here for as long as nobody ran this module.
        self.assertEqual(
            summary["timeouts"]["stream_start_seconds"],
            _declared_stream_start_seconds(),
        )
        self.assertEqual(summary["timeouts"]["recovery_max_seconds"], 14400.0)
        self.assertEqual(summary["tools"]["count"], 2)
        self.assertIn("web_search", summary["tools"]["types"])
        self.assertIn("lookup_order", summary["tools"]["names"])
        self.assertTrue(summary["tools"]["has_web_search_tool"])

    def test_trace_request_summary_reports_effective_additional_tools_without_schemas(self) -> None:
        hooks, _ = load_hook_module()

        summary = hooks._trace_request_summary(
            {
                "model": "default-chat",
                "input": [
                    {
                        "type": "additional_tools",
                        "role": "developer",
                        "tools": [
                            {
                                "type": "custom",
                                "name": "exec",
                                "description": "SECRET_TOOL_DESCRIPTION",
                            },
                            {
                                "type": "namespace",
                                "name": "collaboration",
                                "tools": [
                                    {
                                        "type": "function",
                                        "name": "spawn_agent",
                                        "parameters": {
                                            "secret_schema": "SECRET_TOOL_SCHEMA"
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {"role": "user", "content": "Use the available tools."},
                ],
                "tools": [],
                "tool_choice": {"type": "custom", "name": "exec"},
                "parallel_tool_calls": False,
            }
        )

        tools = summary["tools"]
        self.assertEqual(tools["count"], 2)
        self.assertEqual(tools["top_level_count"], 0)
        self.assertEqual(tools["additional_tools_count"], 2)
        self.assertEqual(tools["origins"], ["additional_tools"])
        self.assertEqual(
            summary["timeouts"]["stream_start_seconds"],
            _declared_stream_start_seconds(),
        )
        self.assertIn("custom", tools["types"])
        self.assertIn("namespace", tools["types"])
        self.assertIn("exec", tools["names"])
        self.assertIn("collaboration", tools["names"])
        self.assertIn("spawn_agent", tools["names"])
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("SECRET_TOOL_DESCRIPTION", serialized)
        self.assertNotIn("SECRET_TOOL_SCHEMA", serialized)

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
                        "name": "web_search",
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
        self.assertIn("web_search", summary["names"])
        self.assertIn("custom_lookup", summary["names"])
        self.assertIn("web_search", summary["names"])
        self.assertIn("lookup_order", summary["names"])
        self.assertIn("web_search_call", summary["types"])

    def test_trace_tool_call_summary_keeps_safe_range_metadata_without_path_values(self) -> None:
        hooks, _ = load_hook_module()

        summary = hooks._trace_tool_call_summary(
            {
                "output": [
                    {
                        "type": "function_call",
                        "id": "call_read",
                        "name": "read_file",
                        "arguments": json.dumps(
                            {
                                "path": "/private/example/source.js",
                                "line_start": 20,
                                "line_end": 48,
                                "limit": 29,
                            }
                        ),
                    }
                ]
            }
        )

        call = summary["calls"][0]
        self.assertEqual(call["argument_keys"], ["path", "line_start", "line_end", "limit"])
        self.assertEqual(
            call["numeric_ranges"],
            {"line_start": 20, "line_end": 48, "limit": 29},
        )

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
        hook = hooks.YoungRouterHook()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
            end = start + timedelta(milliseconds=321)
            kwargs = {
                "call_type": "aresponses",
                "model": "default-chat",
                "completion_start_time": start + timedelta(milliseconds=123),
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
                            "model_group": "public-chat",
                        },
                    },
                },
                "response_cost": 0.0123,
            }
            response = {"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}

            await hook.async_log_success_event(kwargs, response, start, end)

            raw = log_path.read_text(encoding="utf-8")
            record = json.loads(raw)
            self.assertEqual(record["ts"], "2026-06-09T12:00:00Z")
            self.assertEqual(record["status"], "success")
            self.assertEqual(record["duration_ms"], 321)
            self.assertEqual(record["time_to_first_token_ms"], 123)
            self.assertEqual(
                record["time_to_first_token_source"],
                "litellm_completion_start_time",
            )
            self.assertEqual(
                record["first_stream_output_at"],
                "2026-06-09T12:00:00Z",
            )
            self.assertEqual(record["model_group"], "default-chat")
            self.assertEqual(record["public_model"], "public-chat")
            self.assertEqual(record["upstream_model"], "openai/gpt-upstream")
            self.assertEqual(record["deployment_id"], "deployment-1")
            self.assertNotIn("deployment_token", record)
            self.assertEqual(
                record["route_key"],
                "model=public-chat / provider=provider-a / upstream=openai/gpt-upstream / host=provider.example / order=2",
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

    async def test_pre_call_publishes_pending_request_before_completion(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        hooks._REQUEST_STARTED_TIMES.clear()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            kwargs = {
                "call_type": "aresponses",
                "model": "public-chat",
                "litellm_call_id": "pending-call-1",
                "messages": [{"role": "user", "content": "SECRET_PROMPT_BODY"}],
                "api_key": "SECRET_API_KEY",
            }

            await hook.async_pre_call_deployment_hook(kwargs, call_type="aresponses")

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(1, len(records))
            pending = records[0]
            self.assertEqual("pending", pending["status"])
            self.assertEqual("pending-call-1", pending["request_id"])
            self.assertEqual(pending["started_at"], pending["ts"])
            self.assertNotIn("duration_ms", pending)
            self.assertNotIn("SECRET_PROMPT_BODY", log_path.read_text(encoding="utf-8"))
            self.assertNotIn("SECRET_API_KEY", log_path.read_text(encoding="utf-8"))

    async def test_pre_call_settles_an_abandoned_pending_request(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            self.set_env("YOUNG_ROUTER_LOG_REQUEST_STALE_SECONDS", "0.05")
            kwargs = {
                "call_type": "aresponses",
                "model": "public-chat",
                "litellm_call_id": "abandoned-call",
            }

            await hook.async_pre_call_deployment_hook(kwargs, call_type="aresponses")
            await asyncio.sleep(0.2)

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(["pending", "aborted"], [record["status"] for record in records])
            self.assertEqual("abandoned-call", records[-1]["request_id"])
            self.assertEqual(
                "no_terminal_callback",
                records[-1]["aborted"]["reason"],
            )
            # The closed row keeps the attempt's own timestamp so it stays in
            # its chronological place instead of jumping to the newest rows.
            self.assertEqual(records[0]["ts"], records[-1]["ts"])
            self.assertIn("settled_at", records[-1]["aborted"])

    async def test_a_heartbeat_does_not_orphan_the_settlement_watch(self) -> None:
        """A still-reporting stream must still be closable when it stops.

        The heartbeat rewrites the progress row.  Treated as a fresh start it
        would take a new attempt serial, the watch scheduled for the original
        start would no longer recognise the row, and the stream that later went
        silent would never be closed -- the very state the watch exists for.
        """

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            self.set_env("YOUNG_ROUTER_LOG_REQUEST_STALE_SECONDS", "0.05")
            kwargs = {
                "call_type": "aresponses",
                "model": "public-chat",
                "litellm_call_id": "heartbeat-call",
            }

            await hook.async_pre_call_deployment_hook(kwargs, call_type="aresponses")
            hooks._touch_recent_request("heartbeat-call", interval_seconds=0)
            await asyncio.sleep(0.2)

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertIn("aborted", [record["status"] for record in records])
            with hooks._PENDING_RECENT_REQUESTS_LOCK:
                self.assertNotIn("heartbeat-call", hooks._PENDING_RECENT_REQUESTS)

    async def test_a_retry_start_is_not_closed_by_the_earlier_attempt_watch(
        self,
    ) -> None:
        """A second attempt under one request id gets its own stale window.

        A retry republishes the same request id.  The watch the first attempt
        scheduled must leave that newer row alone: closing it would report the
        retry as stale the instant it started.
        """

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            self.set_env("YOUNG_ROUTER_LOG_REQUEST_STALE_SECONDS", "0.3")
            kwargs = {
                "call_type": "aresponses",
                "model": "public-chat",
                "litellm_call_id": "retry-call",
            }

            await hook.async_pre_call_deployment_hook(kwargs, call_type="aresponses")
            # A failover round republishes the attempt under the same id.
            await asyncio.sleep(0.05)
            record = hooks._request_log_record("pending", kwargs)
            hooks._append_recent_request(record)
            await asyncio.sleep(0.05)

            # The first watch expires before the second attempt's window does.
            self.assertFalse(
                hooks._settle_recent_request("retry-call", "no_terminal_callback", attempt=1)
            )
            with hooks._PENDING_RECENT_REQUESTS_LOCK:
                self.assertIn("retry-call", hooks._PENDING_RECENT_REQUESTS)

    async def test_the_settlement_watch_does_not_pin_the_request_body(self) -> None:
        """One watch per attempt may not keep that attempt's replay alive.

        ``asyncio`` gives every task a snapshot of the caller's context
        variables.  The watch is created from inside a request whose context
        holds the replay -- a multi-megabyte body with its images -- and it
        sleeps for the whole stale window, so the snapshot kept the body alive
        for fifteen minutes per attempt.  Measured on the gateway 2026-11-12: a
        worker at 380 MB idle held 1.8 GB of anonymous heap after a few hundred
        Codex turns, and eight of them took the host past its memory.
        """

        hooks, _ = load_hook_module()
        state = sys.modules["young_router.proxy.state"]
        big = {"input": ["x" * 200_000], "messages": [{"role": "user", "content": "y" * 200_000}]}
        with tempfile.TemporaryDirectory() as tmp:
            self.set_log_env(Path(tmp) / "recent-requests.jsonl")
            self.set_env("YOUNG_ROUTER_LOG_REQUEST_STALE_SECONDS", "60")
            token = hooks._CURRENT_ROUTING_REQUEST_KWARGS.set(big)
            try:
                inline = asyncio.get_running_loop().create_task(asyncio.sleep(60))
                hooks._schedule_recent_request_settlement(
                    {"request_id": "settle-context", "status": "pending"}
                )
            finally:
                hooks._CURRENT_ROUTING_REQUEST_KWARGS.reset(token)
            watches = [
                task for task in state._PENDING_SETTLEMENT_TASKS
                if task is not inline
            ]
            self.assertTrue(watches, "the settlement watch was not scheduled")
            for task in watches + [inline]:
                task.cancel()
            if not hasattr(inline, "get_context"):
                self.skipTest("this interpreter does not expose a task's context")
            # The control states the mechanism: an ordinary task does carry it.
            self.assertIs(big, inline.get_context().get(hooks._CURRENT_ROUTING_REQUEST_KWARGS))
            for task in watches:
                self.assertIsNone(
                    task.get_context().get(hooks._CURRENT_ROUTING_REQUEST_KWARGS),
                    "the settlement watch inherited the request body",
                )

    async def test_a_client_that_stops_reading_closes_its_row_at_once(self) -> None:
        """Pressing stop is an ending no callback reports.

        LiteLLM's stream reader fires the failure callback only for an error it
        saw itself, so a closed downstream body left the progress row saying
        发送中 until the stale window (fifteen minutes by default) closed it.  The
        client-facing generator is the one place that knows nobody is reading
        any more, so its ending closes the row.
        """

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        class _StopsMidAnswer:
            def __init__(self) -> None:
                self._chunks = [
                    {"type": "response.output_text.delta", "delta": "partial"}
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._chunks:
                    raise StopAsyncIteration
                return self._chunks.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            request_data = {
                "call_type": "aresponses",
                "model": "public-chat",
                "input": [{"role": "user", "content": "hello"}],
                "stream": True,
                "litellm_call_id": "stopped-call",
            }
            await hook.async_pre_call_deployment_hook(
                request_data, call_type="aresponses"
            )

            stream = hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=_StopsMidAnswer(),
                request_data=request_data,
            )
            # The router opens the turn before the upstream speaks, so the
            # answer is the third frame of the turn.
            await stream.__anext__()  # response.created
            await stream.__anext__()  # response.in_progress
            self.assertEqual(
                {"type": "response.output_text.delta", "delta": "partial"},
                await stream.__anext__(),
            )
            # The reader closes the body: the generator is finalised, exactly as
            # it is when the client disconnects mid-answer.
            await stream.aclose()

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual("aborted", records[-1]["status"])
            self.assertEqual(
                "client_disconnect", records[-1]["aborted"]["reason"]
            )
            with hooks._PENDING_RECENT_REQUESTS_LOCK:
                self.assertNotIn("stopped-call", hooks._PENDING_RECENT_REQUESTS)

    async def test_a_stream_that_ends_by_itself_is_not_a_client_stop(self) -> None:
        """A finished stream closes through its callback, not as a stop.

        The generator also ends when nothing is reading yet -- after a complete
        Chat turn, where no Responses terminal frame exists to deliver.  That
        ending is not a person pressing stop, so it must leave the row to the
        success callback instead of writing an interrupted row beside it.
        """

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        class _AnswersAndEnds:
            def __init__(self) -> None:
                self._chunks = [
                    {"type": "response.output_text.delta", "delta": "done"}
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._chunks:
                    raise StopAsyncIteration
                return self._chunks.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            request_data = {
                "call_type": "aresponses",
                "model": "public-chat",
                "input": [{"role": "user", "content": "hello"}],
                "stream": True,
                "litellm_call_id": "finished-call",
            }
            await hook.async_pre_call_deployment_hook(
                request_data, call_type="aresponses"
            )

            stream = hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=_AnswersAndEnds(),
                request_data=request_data,
            )
            chunks = [chunk async for chunk in stream]
            self.assertTrue(chunks)

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                ["pending"], [record["status"] for record in records]
            )
            # The turn is still open for its terminal callback.
            with hooks._PENDING_RECENT_REQUESTS_LOCK:
                self.assertIn("finished-call", hooks._PENDING_RECENT_REQUESTS)

    async def test_a_stream_that_fails_is_closed_as_a_failed_request(self) -> None:
        """A stream that ended in an error states the error, not an interruption.

        LiteLLM reports a stream that fails through its own post-call hook, not
        through the logging callback this proxy writes request rows from, so on
        the Messages and Chat wires nothing followed the progress row.  It then
        read 发送中 until the stale window renamed it 已中断 -- an interruption
        for a turn whose recorded failure named the upstream.
        """

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0")

        class UpstreamBadGateway(Exception):
            status_code = 502

        class _FailsDuringStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise UpstreamBadGateway("upstream request failed")

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            request_data = {
                "call_type": "amessages",
                "model": "public-chat",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "litellm_call_id": "failed-stream-call",
            }
            await hook.async_pre_call_deployment_hook(
                request_data, call_type="amessages"
            )

            with self.assertRaises(Exception):
                async for _chunk in hook.async_post_call_streaming_iterator_hook(
                    user_api_key_dict=None,
                    response=_FailsDuringStream(),
                    request_data=request_data,
                ):
                    pass

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual("failed-stream-call", records[0]["request_id"])
            self.assertEqual("pending", records[0]["status"])
            self.assertEqual("failure", records[-1]["status"])
            self.assertEqual("failed-stream-call", records[-1]["request_id"])
            self.assertEqual("upstream-status-502", records[-1]["error"]["reason"])
            self.assertEqual(502, records[-1]["error"]["status_code"])
            self.assertIn("duration_ms", records[-1])
            with hooks._PENDING_RECENT_REQUESTS_LOCK:
                self.assertNotIn("failed-stream-call", hooks._PENDING_RECENT_REQUESTS)

    async def test_a_failed_hop_the_turn_recovered_from_is_still_a_failed_request(self) -> None:
        """A mid-stream refusal is a logged failure even when a sibling serves.

        LiteLLM's failure callback never runs for a stream that failed after it
        started, and the turn that recovers on the next route reports only its
        success.  Without the hop's own row, 请求 showed nothing while 路由跟踪
        showed the failure -- and the failed attempt's row read 发送中 until the
        stale window renamed it 已中断.  Closing the attempt where this router
        abandons it is what keeps the two tabs telling the same story.
        """

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        class UpstreamBadGateway(Exception):
            status_code = 502

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            first = {
                "call_type": "amessages",
                "model": "public-chat",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "litellm_call_id": "hop-turn",
                "model_info": {"id": "hop-a", "model_group": "public-chat"},
            }
            await hook.async_pre_call_deployment_hook(first, call_type="amessages")
            hooks._mark_exception_for_deployment_failover(
                UpstreamBadGateway("upstream session rejected"),
                first,
            )

            # The refusal is kept, not written: a reader closing the body at
            # this instant is not a route failure, and only a new attempt (or a
            # terminal stop) proves the router moved on.
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(["pending"], [record["status"] for record in records])

            # The sibling that serves the turn publishes its own row beside the
            # failed hop instead of replacing it.
            second = {
                **first,
                "model_info": {"id": "hop-b", "model_group": "public-chat"},
            }
            await hook.async_pre_call_deployment_hook(second, call_type="amessages")
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                ["pending", "failure", "pending"],
                [record["status"] for record in records],
            )
            self.assertEqual("hop-a", records[1]["deployment_id"])
            self.assertEqual("upstream-status-502", records[1]["error"]["reason"])
            self.assertEqual("hop-b", records[-1]["deployment_id"])

    def test_the_attempt_row_id_reaches_the_streaming_request(self) -> None:
        """A rebuilt request body keeps the turn's identity.

        LiteLLM hands the post-call streaming hook a body it rebuilt, and that
        body carries no ``litellm_call_id``.  Every trace the stream guard wrote
        from it (a failover, a declined cooldown) therefore had no request to
        belong to, so the trace pane could only list it as a standalone request
        that failed beside the request it happened in.  The pre-call hook pins
        the attempt's row on the selected route marker, and applying that marker
        stamps the same id on the body.
        """

        hooks, _ = load_hook_module()
        # The selected route is a context variable, and this test runs outside
        # an isolated task: leaving it set would hand the next test a marker
        # that belongs to this one's request.
        previous_deployment = hooks._CURRENT_SELECTED_DEPLOYMENT.get()
        previous_box = hooks._CURRENT_SELECTED_DEPLOYMENT_BOX.get()
        self.addCleanup(hooks._CURRENT_SELECTED_DEPLOYMENT.set, previous_deployment)
        self.addCleanup(hooks._CURRENT_SELECTED_DEPLOYMENT_BOX.set, previous_box)
        deployment = {
            "litellm_params": {
                "model": "anthropic/kimi-k3",
                "api_base": "https://chat-provider.example/v1",
            },
            "model_info": {"id": "kimi-chat-route", "model_group": "kimi-k3"},
        }
        hooks._remember_selected_deployment(deployment, surface="anthropic")
        hooks._remember_pending_request_log_id("turn-row-1")
        request_kwargs = {"call_type": "amessages", "model": "kimi-k3", "messages": []}

        hooks._remember_selected_deployment_for_request(request_kwargs, deployment)

        self.assertEqual("turn-row-1", hooks._trace_request_id(request_kwargs))
        self.assertEqual("kimi-chat-route", request_kwargs["model_info"]["id"])
        # The trace a failover writes from this body now names its request, so
        # the trace pane attaches the hop to the turn instead of listing it as a
        # standalone request that failed beside it.
        with self.assertLogs("young_router.route_trace", level="WARNING") as captured:
            hooks._mark_exception_for_deployment_failover(
                RuntimeError("upstream request failed"),
                request_kwargs,
            )
        traces = [
            json.loads(line.split("litellm_route_trace ", 1)[1])
            for line in captured.output
            if "litellm_route_trace " in line
        ]
        self.assertIn("deployment_failover_marked", [trace.get("event") for trace in traces])
        self.assertEqual(
            {"turn-row-1"},
            {trace.get("request_id") for trace in traces},
        )

    async def test_a_failure_the_callback_already_reported_is_not_written_twice(self) -> None:
        """The logging callback is the same ending, reported once.

        LiteLLM's own failure callback closes the in-flight row.  A stream
        teardown that follows it must find nothing to close instead of writing a
        second failure row for the same attempt.
        """

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            kwargs = {
                "call_type": "amessages",
                "model": "public-chat",
                "stream": True,
                "litellm_call_id": "callback-failed-call",
            }
            await hook.async_pre_call_deployment_hook(
                kwargs, call_type="amessages"
            )
            await hook.async_log_failure_event(
                kwargs,
                RuntimeError("upstream route failed"),
                start,
                start + timedelta(milliseconds=10),
            )
            settled = hooks._settle_failed_attempt(
                {"call_type": "amessages", "litellm_call_id": "callback-failed-call"},
                RuntimeError("upstream route failed"),
            )

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertFalse(settled)
            self.assertEqual(
                ["pending", "failure"], [record["status"] for record in records]
            )

    async def test_terminal_callback_keeps_its_pending_row_open(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            self.set_env("YOUNG_ROUTER_LOG_REQUEST_STALE_SECONDS", "0.05")
            start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
            kwargs = {
                "call_type": "aresponses",
                "model": "public-chat",
                "litellm_call_id": "settled-call",
            }

            await hook.async_pre_call_deployment_hook(kwargs, call_type="aresponses")
            await hook.async_log_failure_event(
                kwargs,
                RuntimeError("upstream failed"),
                start,
                start + timedelta(milliseconds=10),
            )
            await asyncio.sleep(0.2)

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(["pending", "failure"], [record["status"] for record in records])

    async def test_stream_progress_refreshes_the_pending_row(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            kwargs = {
                "call_type": "aresponses",
                "model": "public-chat",
                "litellm_call_id": "streaming-call",
            }

            await hook.async_pre_call_deployment_hook(kwargs, call_type="aresponses")
            hooks._touch_recent_request("streaming-call", interval_seconds=0)

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(["pending", "stream"], [record["status"] for record in records])
            self.assertEqual("streaming-call", records[-1]["request_id"])
            self.assertIs(records[-1]["heartbeat"], True)

    async def test_selected_deployment_marker_carries_the_pending_request_row(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            marker = {"model_info": {"id": "deployment-1"}}
            hooks._CURRENT_SELECTED_DEPLOYMENT.set(marker)
            kwargs = {
                "call_type": "aresponses",
                "model": "public-chat",
                "litellm_call_id": "marked-call",
            }

            await hook.async_pre_call_deployment_hook(kwargs, call_type="aresponses")

            self.assertEqual(
                "marked-call",
                marker.get("_young_router_pending_request_log_id"),
            )

    async def test_selected_deployment_stream_refreshes_its_pending_row(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            kwargs = {
                "call_type": "aresponses",
                "model": "public-chat",
                "litellm_call_id": "marked-call",
            }
            await hook.async_pre_call_deployment_hook(kwargs, call_type="aresponses")

            class _Chunks:
                def __init__(self) -> None:
                    self._chunks = [{"type": "response.output_text.delta"}]

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if not self._chunks:
                        raise StopAsyncIteration
                    return self._chunks.pop(0)

            touched: list[str] = []
            hooks._touch_recent_request = lambda request_id, **kwargs: touched.append(request_id)
            wrapped = hooks._wrap_response_with_selected_deployment_marker(
                _Chunks(),
                {
                    "model_info": {"id": "deployment-1"},
                    "_young_router_pending_request_log_id": "marked-call",
                },
            )

            chunk = await wrapped.__anext__()

            self.assertEqual({"type": "response.output_text.delta"}, chunk)
            self.assertEqual(["marked-call"], touched)

    def test_boot_settlement_closes_previous_pending_rows(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            records = [
                {"ts": "2026-06-09T12:00:00Z", "request_id": "gone", "status": "pending"},
                {"ts": "2026-06-09T12:00:01Z", "request_id": "finished", "status": "success"},
                {"ts": "2026-06-09T12:00:02Z", "request_id": "joined", "status": "pending"},
                {"ts": "2026-06-09T12:00:03Z", "request_id": "joined", "status": "success"},
            ]
            log_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            settled = hooks.settle_stale_recent_requests(reason="service_restart")

            self.assertEqual(1, settled)
            written = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(5, len(written))
            self.assertEqual("gone", written[-1]["request_id"])
            self.assertEqual("aborted", written[-1]["status"])
            self.assertEqual("service_restart", written[-1]["aborted"]["reason"])
            self.assertEqual("2026-06-09T12:00:00Z", written[-1]["ts"])

    async def test_stream_request_log_advances_to_terminal_status(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
            kwargs = {
                "call_type": "aresponses",
                "model": "public-chat",
                "stream": True,
                "litellm_call_id": "stream-call-1",
            }

            await hook.async_log_success_event(
                kwargs,
                {"type": "response.created", "response": {"status": "in_progress"}},
                start,
                start + timedelta(milliseconds=10),
            )
            await hook.async_log_stream_event(
                kwargs,
                {"type": "response.completed", "response": {"status": "completed"}},
                start,
                start + timedelta(milliseconds=30),
            )
            failed_kwargs = {**kwargs, "litellm_call_id": "stream-call-2"}
            await hook.async_log_stream_event(
                failed_kwargs,
                {"type": "response.failed", "response": {"status": "failed"}},
                start,
                start + timedelta(milliseconds=40),
            )

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(["stream", "success", "failure"], [record["status"] for record in records])

    async def test_completed_stream_summary_logs_success(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
            kwargs = {
                "model": "public-chat",
                "stream": True,
                "litellm_call_id": "completed-stream-call",
                "async_complete_streaming_response": {"id": "response-summary"},
            }

            await hook.async_log_success_event(kwargs, {"id": "response-summary"}, now, now)

            record = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual("success", record["status"])

    def test_request_log_uses_router_public_model_after_upstream_rewrite(self) -> None:
        hooks, _ = load_hook_module()

        record = hooks._request_log_record(
            "success",
            {
                "model": "vendor-chat-tagged",
                "custom_llm_provider": "provider-a",
                "litellm_params": {
                    "model": "openai/vendor-chat-tagged",
                    "api_base": "https://provider.example/v1",
                    "metadata": {
                        "deployment_model_name": "public-chat",
                        "model_info": {
                            "id": "deployment-1",
                            "provider": "provider-a",
                        },
                    },
                },
            },
        )

        self.assertEqual("public-chat", record["public_model"])
        self.assertEqual("openai/vendor-chat-tagged", record["upstream_model"])
        self.assertTrue(record["route_key"].startswith("model=public-chat /"))
        self.assertEqual("selected", record["routing_state"])

    def test_request_log_states_the_inbound_and_outbound_wire(self) -> None:
        """The two protocol columns read the wires, never the declaration.

        A ``fallback`` route answers the client's own /v1/responses request
        while declaring a chat backup, so the row has to name the wire the
        attempt actually left on.  An inbound protocol is the client's own
        door, which is why a Messages client bridged onto a chat route reads
        ``Messages`` in one column and ``Chat`` in the other: those two facts
        are what tell a reader where a bridged turn was adapted.
        """

        hooks, _ = load_hook_module()
        routing = sys.modules["young_router.proxy.routing"]
        self.addCleanup(routing._ADAPTED_SURFACE_REQUESTS.clear)

        responses = hooks._request_log_record(
            "success",
            {
                "model": "public-chat",
                "request_id": "req-wire-log-1",
                "input": [{"role": "user", "content": "hello"}],
                "proxy_server_request": {
                    "url": "http://127.0.0.1:4000/v1/responses",
                },
                "model_info": {
                    "id": "deployment-1",
                    # The route declares its backup protocol; the surface below
                    # is the one this attempt was dispatched on.
                    "upstream_url_surface": "openai/chat",
                },
                "_young_router_upstream_url_surface": "openai/responses",
            },
        )
        self.assertEqual("responses", responses["client_surface"])
        self.assertEqual("responses", responses["upstream_surface"])

        # A Messages turn adapted onto a chat route: the terminal callback's
        # kwargs carry no surface of their own, so the request id's memory of
        # the adapted wire is what has to answer.
        routing._remember_request_adapted_surface(
            {"request_id": "req-wire-log-2"}, "openai/chat"
        )
        bridged = hooks._request_log_record(
            "success",
            {
                "call_type": "anthropic_messages",
                "request_id": "req-wire-log-2",
                "model_info": {"id": "deployment-2"},
            },
        )
        self.assertEqual("messages", bridged["client_surface"])
        self.assertEqual("chat", bridged["upstream_surface"])

    def test_request_log_keeps_the_inbound_wire_of_an_adapted_body(self) -> None:
        """A body a route's adapter already rewrote still reports its own door.

        LiteLLM's deployment hook hands over the call's kwargs rebuilt from the
        deployment parameters, so a Responses turn on a Chat-shaped route
        arrives with ``messages`` and no ``input``.  Read at face value that
        body called in on Chat, and the Log tab showed a Responses client's row
        as inbound Chat -- the one column that exists to tell a bridged turn
        from a native one.  The copy LiteLLM takes on arrival, before any hook
        touched it, is still under ``proxy_server_request.body``.
        """

        hooks, _ = load_hook_module()
        record = hooks._request_log_record(
            "pending",
            {
                "model": "openai/public-chat",
                "messages": [{"role": "user", "content": "hello"}],
                "call_type": None,
                "custom_llm_provider": "openai",
                "use_chat_completions_api": True,
                "proxy_server_request": {
                    "url": "http://127.0.0.1:4000/v1/responses",
                    "body": {
                        "model": "public-chat",
                        "input": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                },
                "model_info": {
                    "id": "deployment-1",
                    "upstream_url_surface": "openai/chat",
                },
                "_young_router_upstream_url_surface": "openai/chat",
            },
        )
        self.assertEqual("responses", record["client_surface"])
        self.assertEqual("chat", record["upstream_surface"])

    def test_a_messages_client_is_never_reported_as_chat(self) -> None:
        """``messages`` is not evidence of a wire; Claude Code sends one too.

        Reading the arrival body for the client's wire fixed a Responses turn
        being drawn as Chat, but a ``messages`` key then made every Claude Code
        turn read 入站 Chat -- a /v1/messages client reported as Chat in the one
        column that says which door it used.  Chat and Anthropic Messages share
        that key, so only the URL, the call type, or an ``input`` body can name
        the wire; the pending row and the terminal row are both covered because
        they carry different evidence (the URL in one, the call type in the
        other).
        """

        hooks, _ = load_hook_module()
        body = {
            "model": "deepseek-v4.1-flash",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": "hello"}],
        }
        pending = hooks._request_log_record(
            "pending",
            {
                "model": "anthropic/deepseek-v4.1-flash",
                "messages": body["messages"],
                "call_type": None,
                "proxy_server_request": {
                    "url": "http://127.0.0.1:4000/v1/messages",
                    "body": body,
                },
                "_young_router_upstream_url_surface": "anthropic",
            },
        )
        self.assertEqual("messages", pending["client_surface"])

        # The terminal callback keeps no proxy request, only the call type.
        terminal = hooks._request_log_record(
            "success",
            {
                "model": "openai/deepseek-flash",
                "messages": body["messages"],
                "call_type": "anthropic_messages",
                "_young_router_upstream_url_surface": "anthropic",
            },
        )
        self.assertEqual("messages", terminal["client_surface"])

    def test_request_log_leaves_an_unknown_protocol_empty(self) -> None:
        """A row that cannot name a wire states none instead of ``unknown``.

        The column answers "which wire carried this turn"; drawn with the word
        ``unknown`` it would report a protocol no client ever called in on.
        """

        hooks, _ = load_hook_module()

        record = hooks._request_log_record(
            "failure",
            {
                "call_type": "/v1/models",
                "exception": RuntimeError("no deployment"),
            },
        )

        self.assertNotIn("client_surface", record)
        self.assertNotIn("upstream_surface", record)

    def test_request_log_states_the_cache_the_upstream_reported(self) -> None:
        """缓存命中 reads the spellings a relay actually answers with.

        A DeepSeek-shaped body states the reused prefix in
        ``prompt_tokens_details.cached_tokens`` *and* a literal zero in
        ``cache_read_input_tokens``; taking the first spelling alone would
        report a cold cache on a warm thread.  A turn whose upstream said
        nothing about its cache states nothing, so the column stays empty
        instead of claiming a measured miss.
        """

        hooks, _ = load_hook_module()

        def success(usage: dict) -> dict:
            response = {"id": "response-cache-log", "usage": dict(usage)}
            return hooks._request_log_record(
                "success",
                {"model": "public-chat", "request_id": "req-cache-log"},
                response,
            )

        warm = success(
            {
                "prompt_tokens": 20000,
                "completion_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 18000},
                "cache_read_input_tokens": 0,
            }
        )
        self.assertEqual(18000, warm["usage"]["cache_read_tokens"])
        self.assertNotIn("cache_write_tokens", warm["usage"])

        written = success(
            {
                "input_tokens": 9000,
                "output_tokens": 40,
                # The cache write is reported by the provider's own spelling.
                "cache_creation_input_tokens": 8216,
            }
        )
        self.assertEqual(8216, written["usage"]["cache_write_tokens"])

        # A count larger than the input it was drawn against cannot be a share
        # of that input, so it is bounded instead of logging a ratio above 1.
        bounded = success(
            {
                "prompt_tokens": 800,
                "completion_tokens": 10,
                "prompt_cache_hit_tokens": 5000,
            }
        )
        self.assertEqual(800, bounded["usage"]["cache_read_tokens"])

        silent = success({"prompt_tokens": 800, "completion_tokens": 10})
        self.assertEqual({}, {key: silent["usage"][key] for key in ("cache_read_tokens", "cache_write_tokens") if key in silent["usage"]})

    def test_request_log_classifies_pre_route_failures_and_trace_reasons(self) -> None:
        hooks, _ = load_hook_module()

        no_deployment = RuntimeError(
            "There are no healthy deployments for this model."
        )
        no_deployment.status_code = 400
        no_deployment_record = hooks._request_log_record(
            "failure",
            {"model": "fresh-model", "exception": no_deployment},
        )
        self.assertEqual(
            no_deployment_record["routing_state"],
            "no_available_deployment",
        )
        self.assertEqual(
            no_deployment_record["error"]["reason"],
            "no-available-deployment",
        )
        self.assertEqual(
            hooks._trace_exception(no_deployment)["reason"],
            "no-available-deployment",
        )

        proxy_model_error_type = type("ProxyModelNotFoundError", (Exception,), {})
        model_not_configured = proxy_model_error_type(
            "No deployment configured for this model"
        )
        model_not_configured_record = hooks._request_log_record(
            "failure",
            {"model": "missing-model", "exception": model_not_configured},
        )
        self.assertEqual(
            model_not_configured_record["routing_state"],
            "model_not_configured",
        )
        self.assertEqual(
            model_not_configured_record["error"]["reason"],
            "model-not-configured",
        )
        self.assertEqual(
            hooks._trace_exception(model_not_configured)["reason"],
            "model-not-configured",
        )

    def test_local_gateway_key_rejection_is_not_an_upstream_failure(self) -> None:
        """LiteLLM's own auth refusal never reaches a route.

        A caller using a stale key reaches this proxy, not a provider: LiteLLM
        looks the key up as a virtual key, needs the database for that, and
        answers ``ProxyException(type="no_db_connection", code=400)``.  Read as
        ``other`` it produced 路由前失败 | 上游异常 in the request log for an
        upstream that was never contacted -- every ``/v1/models`` probe with an
        old key became a row blaming the providers.  The type, not the
        sentence, is what tells this apart from a provider's own database
        error, which stays a route failure.
        """

        hooks, _ = load_hook_module()

        class ProxyException(Exception):
            def __init__(self, message: str, type: str, code: int) -> None:
                super().__init__(message)
                self.type = type
                self.code = str(code)

        rejection = ProxyException("No connected db.", "no_db_connection", 400)

        self.assertTrue(hooks._is_local_gateway_auth_rejection_error(rejection))
        self.assertEqual(
            hooks._trace_exception(rejection)["reason"],
            "local-gateway-auth-rejected",
        )

        record = hooks._request_log_record(
            "failure",
            {"call_type": "/v1/models", "exception": rejection},
        )
        self.assertEqual(record["routing_state"], "gateway_auth_rejected")
        self.assertEqual(record["error"]["reason"], "local-gateway-auth-rejected")

        # A provider that reports its own database problem is a route failure:
        # the same sentence arrives with a provider-owned exception instead.
        provider_failure = ProxyException(
            "No connected db.", "internal_server_error", 500
        )
        self.assertFalse(
            hooks._is_local_gateway_auth_rejection_error(provider_failure)
        )

    async def test_a_control_plane_read_never_becomes_a_request_row(self) -> None:
        """A listing or health read is not a turn, even when it was refused.

        Every ``/v1/models`` read -- a client's picker, this app's own
        client-model loop -- selects no route, so the row it produced carried no
        model, no cost and no usage; the only thing a reader saw was
        本机网关拒绝密钥, repeated once per read while the proxy replaced its
        workers.  The same stale key still has to show on a real turn, and that
        is the boundary this test pins.
        """

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        class ProxyException(Exception):
            def __init__(self, message: str, type: str, code: int) -> None:
                super().__init__(message)
                self.type = type
                self.code = str(code)

        rejection = ProxyException("No connected db.", "no_db_connection", 400)
        start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(milliseconds=3)

        self.assertTrue(hooks._is_local_control_plane_read({"call_type": "/v1/models"}))
        self.assertTrue(hooks._is_local_control_plane_read({"call_type": "/health"}))
        self.assertFalse(hooks._is_local_control_plane_read({"call_type": "aresponses"}))
        self.assertFalse(hooks._is_local_control_plane_read({"call_type": "amodels"}))

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)

            await hook.async_log_failure_event(
                {"call_type": "/v1/models", "exception": rejection}, None, start, end
            )
            await hook.async_log_success_event(
                {"call_type": "/v1/models", "model": "public-chat"},
                {"id": "list"},
                start,
                end,
            )
            await hook.async_log_failure_event(
                {"call_type": "/health", "exception": RuntimeError("probe")},
                None,
                start,
                end,
            )
            written = (
                log_path.read_text(encoding="utf-8").strip()
                if log_path.exists()
                else ""
            )
            self.assertEqual("", written)

            # A real turn carrying the same refused key is still reported: that
            # row is the one that tells a reader whose key is stale.
            await hook.async_log_failure_event(
                {
                    "call_type": "aresponses",
                    "model": "public-chat",
                    "exception": rejection,
                },
                None,
                start,
                end,
            )
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(1, len(records))
            self.assertEqual("gateway_auth_rejected", records[0]["routing_state"])

    def test_recent_request_timestamp_treats_naive_callback_time_as_local(self) -> None:
        hooks, _ = load_hook_module()
        callback_time = datetime(2026, 6, 9, 12, 0, 0)
        expected = callback_time.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        self.assertEqual(hooks._event_time(callback_time), expected)

    async def test_recent_request_failure_log_omits_error_message_body(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

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
            self.assertEqual(record["routing_state"], "unselected")
            self.assertNotIn("SECRET_ERROR_BODY", raw)
            self.assertNotIn("SECRET_STANDARD_ERROR", raw)
            self.assertNotIn("SECRET_PROMPT_BODY", raw)

    def test_ttft_omits_litellm_end_time_fallback(self) -> None:
        hooks, _ = load_hook_module()
        start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(milliseconds=321)

        self.assertIsNone(
            hooks._time_to_first_token_ms(
                {"completion_start_time": end},
                start,
                end,
            )
        )

    def test_ttft_prefers_directly_observed_stream_output(self) -> None:
        hooks, _ = load_hook_module()
        start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        first_output = start + timedelta(milliseconds=87)
        end = start + timedelta(milliseconds=321)
        request_kwargs = {
            "completion_start_time": end,
            hooks._FIRST_STREAM_OUTPUT_TIME_KEY: first_output,
        }

        self.assertEqual(
            hooks._time_to_first_token_ms(request_kwargs, start, end),
            87,
        )
        self.assertEqual(
            hooks._completion_start_time(request_kwargs, end)[1],
            "stream_output_observed",
        )

    def test_ttft_correlates_observed_stream_output_by_request_id(self) -> None:
        hooks, _ = load_hook_module()
        start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        first_output = start + timedelta(milliseconds=64)
        stream_request = {
            "litellm_call_id": "ttft-correlation-test",
            hooks._FIRST_STREAM_OUTPUT_TIME_KEY: first_output,
        }
        callback_kwargs = {
            "litellm_call_id": "ttft-correlation-test",
            "completion_start_time": start + timedelta(milliseconds=321),
        }

        hooks._record_first_stream_output_time(stream_request)

        self.assertEqual(
            hooks._time_to_first_token_ms(
                callback_kwargs,
                start,
                start + timedelta(milliseconds=321),
            ),
            64,
        )

    def test_first_stream_output_time_is_not_forwarded_as_provider_kwarg(self) -> None:
        hooks, _ = load_hook_module()
        request = {"litellm_call_id": "ttft-no-output-datetime"}
        observed = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

        hooks._record_first_stream_output_time(request, observed)

        self.assertNotIn(hooks._FIRST_STREAM_OUTPUT_TIME_KEY, request)
        self.assertEqual(hooks._first_stream_output_time(request), observed)

    def test_ttft_uses_proxy_observed_request_start(self) -> None:
        hooks, _ = load_hook_module()
        started = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        first_output = started + timedelta(milliseconds=91)
        stream_request = {
            "litellm_call_id": "ttft-request-start-test",
            hooks._REQUEST_STARTED_TIME_KEY: started,
            hooks._FIRST_STREAM_OUTPUT_TIME_KEY: first_output,
        }
        callback_kwargs = {
            "litellm_call_id": "ttft-request-start-test",
            "completion_start_time": first_output,
        }

        hooks._record_request_started_time(stream_request)
        hooks._record_first_stream_output_time(stream_request)

        self.assertEqual(
            hooks._time_to_first_token_ms(
                callback_kwargs,
                first_output,
                first_output + timedelta(milliseconds=200),
            ),
            91,
        )

    def test_request_start_correlation_keeps_earliest_pre_call(self) -> None:
        hooks, _ = load_hook_module()
        first = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        later = first + timedelta(seconds=5)

        hooks._record_request_started_time(
            {
                "litellm_call_id": "ttft-earliest-start-test",
                hooks._REQUEST_STARTED_TIME_KEY: first,
            }
        )
        hooks._record_request_started_time(
            {
                "litellm_call_id": "ttft-earliest-start-test",
                hooks._REQUEST_STARTED_TIME_KEY: later,
            }
        )

        self.assertEqual(
            hooks._request_started_time(
                {"litellm_call_id": "ttft-earliest-start-test"}
            ),
            first,
        )

    def test_request_start_time_is_not_forwarded_as_provider_kwarg(self) -> None:
        hooks, _ = load_hook_module()
        request = {"litellm_call_id": "ttft-no-private-datetime"}

        hooks._record_request_started_time(request)

        self.assertNotIn(hooks._REQUEST_STARTED_TIME_KEY, request)
        self.assertIsNotNone(hooks._request_started_time(request))

    def test_recent_request_rotation_keeps_bounded_current_and_backup_tail(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            cap = hooks.MIN_LOG_MAX_BYTES
            log_path.write_text("a" * (cap + 1024), encoding="utf-8")
            self.set_log_env(log_path)
            self.set_env("YOUNG_ROUTER_LOG_MAX_BYTES", str(cap))

            hooks._append_recent_request({"status": "success", "marker": "latest"})

            self.assertLessEqual(log_path.stat().st_size, 128)
            backup_path = Path(f"{log_path}.1")
            self.assertTrue(backup_path.exists())
            self.assertLessEqual(backup_path.stat().st_size, cap)
            self.assertIn('"marker": "latest"', log_path.read_text(encoding="utf-8"))
            self.assertEqual(backup_path.read_text(encoding="utf-8"), "a" * cap)

    def test_recent_request_rotation_uses_local_log_cap_key(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env("YOUNG_ROUTER_LOG_MAX_BYTES", "300000")

        self.assertEqual(hooks._recent_requests_max_bytes(), 300000)

    async def test_deployment_failover_trace_records_route_key(self) -> None:
        hooks, _ = load_hook_module()

        class TemporaryFailure(Exception):
            status_code = 503

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

        with self.assertLogs("young_router.route_trace", level="WARNING") as captured:
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        raw_payload = captured.output[0].split("litellm_route_trace ", 1)[1]
        record = json.loads(raw_payload)
        self.assertEqual(record["event"], "deployment_failover_marked")
        self.assertRegex(record["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertNotIn("deployment_token", record)
        self.assertEqual(record["route_key"], "provider_alpha / openai/default-chat / key=team / order=1")
        self.assertEqual(
            record["exception"]["failed_deployment_route_key"],
            "provider_alpha / openai/default-chat / key=team / order=1",
        )

    def test_request_log_traces_buffered_text_only_completion_with_requested_tools(self) -> None:
        hooks, _ = load_hook_module()
        start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(seconds=30)
        kwargs = {
            "model": "public-agent",
            "stream": True,
            "litellm_params": {
                "model": "openai/public-agent",
                "api_base": "https://relay.example/v1",
                "metadata": {
                    "deployment_model_name": "public-agent",
                    "model_info": {"id": "deployment-7", "provider": "relay"},
                },
            },
            "tools": [
                {"type": "function", "function": {"name": "bash"}},
            ],
            # The relay buffered the whole generation and flushed it at the
            # end: the first token arrived 29.5s into a 30s request.
            "completion_start_time": start + timedelta(milliseconds=29500),
        }
        text_only_response = {
            "type": "message",
            "content": [{"type": "output_text", "text": "Let me check the state."}],
        }

        with self.assertLogs("young_router.route_trace", level="WARNING") as captured:
            record = hooks._request_log_record(
                "success", kwargs, text_only_response, start, end
            )

        self.assertEqual("success", record["status"])
        trace = json.loads(captured.output[0].split("litellm_route_trace ", 1)[1])
        self.assertEqual(
            trace["event"],
            "responses_completed_text_only_after_buffered_stream",
        )
        self.assertEqual(trace["model_group"], "public-agent")
        self.assertEqual(trace["deployment_id"], "deployment-7")
        self.assertEqual(trace["duration_ms"], 30000)
        self.assertEqual(trace["time_to_first_token_ms"], 29500)
        self.assertIn("output_text", trace["response_types"])
        self.assertNotIn("function_call", trace["response_types"])
        self.assertEqual(trace["tool_types"], ["function"])

    def test_request_log_no_trace_when_completion_carries_function_call_or_early_token(self) -> None:
        hooks, _ = load_hook_module()
        start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(milliseconds=32657)
        base_kwargs = {
            "model": "public-agent",
            "stream": True,
            "litellm_params": {
                "model": "openai/public-agent",
                "api_base": "https://relay.example/v1",
                "metadata": {
                    "deployment_model_name": "public-agent",
                    "model_info": {"id": "deployment-7", "provider": "relay"},
                },
            },
            "tools": [
                {"type": "function", "function": {"name": "bash"}},
            ],
        }
        with_tool_call_response = {
            "type": "message",
            "content": [{"type": "output_text", "text": "running"}],
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "bash",
                    "arguments": "{}",
                }
            ],
        }
        with self.assertNoLogs("young_router.route_trace", level="WARNING"):
            hooks._request_log_record(
                "success",
                {**base_kwargs, "completion_start_time": start + timedelta(milliseconds=29500)},
                with_tool_call_response,
                start,
                end,
            )
        # First token streamed early: not the buffered-flush signature.
        with self.assertNoLogs("young_router.route_trace", level="WARNING"):
            hooks._request_log_record(
                "success",
                {**base_kwargs, "completion_start_time": start + timedelta(milliseconds=500)},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "early stream"}],
                },
                start,
                end,
            )

    def test_a_served_route_states_the_wire_it_answered_on(self) -> None:
        """The success record must name the wire, not the backup protocol.

        A ``fallback`` route that answers the client's own /v1/responses request
        still declares ``openai/chat`` as its backup.  Reading that declaration
        as the wire labelled live Responses traffic as a chat call and split the
        served route from its own selection in the viewer.
        """

        hooks, _ = load_hook_module()
        routing = sys.modules["young_router.proxy.routing"]
        kwargs = {
            "model": "public-chat",
            "request_id": "req-wire-1",
            "custom_llm_provider": "provider_alpha",
            "litellm_params": {
                "model": "openai/upstream-chat",
                "api_base": "https://relay.example/v1",
            },
            "model_info": {
                "id": "deployment-9",
                "provider": "provider_alpha",
                "api_key_name": "team",
                "order": 2,
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
            "_young_router_upstream_url_surface": "openai/responses",
        }
        with routing._TRACED_DEPLOYMENT_SUCCESSES_LOCK:
            routing._TRACED_DEPLOYMENT_SUCCESSES.clear()
        self.addCleanup(routing._TRACED_DEPLOYMENT_SUCCESSES.clear)

        with self.assertLogs("young_router.route_trace", level="WARNING") as captured:
            routing._record_deployment_success_for_cooldown(kwargs)

        record = json.loads(captured.output[0].split("litellm_route_trace ", 1)[1])
        self.assertEqual("deployment_success", record["event"])
        self.assertEqual("openai/responses", record["deployment"]["surface"])
        self.assertEqual("openai/chat", record["deployment"]["upstream_url_surface"])
        self.assertEqual("fallback", record["deployment"]["upstream_protocol_mode"])

    def test_a_served_route_is_traced_once_per_request(self) -> None:
        """One success trace per request, naming the route that served it.

        A picked route is only a plan, so the route trace has to learn from the
        success path which deployment actually answered. Several success paths
        call the shared funnel for the same request, and each of them would
        otherwise repeat the record.
        """

        hooks, _ = load_hook_module()
        routing = sys.modules["young_router.proxy.routing"]
        kwargs = {
            "model": "public-agent",
            "request_id": "req-success-1",
            "custom_llm_provider": "provider_alpha",
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://relay.example/v1",
            },
            "model_info": {
                "id": "deployment-9",
                "provider": "provider_alpha",
                "api_key_name": "team",
                "order": 2,
            },
        }
        with routing._TRACED_DEPLOYMENT_SUCCESSES_LOCK:
            routing._TRACED_DEPLOYMENT_SUCCESSES.clear()
        self.addCleanup(routing._TRACED_DEPLOYMENT_SUCCESSES.clear)

        with self.assertLogs("young_router.route_trace", level="WARNING") as captured:
            routing._record_deployment_success_for_cooldown(kwargs)
            routing._record_deployment_success_for_cooldown(dict(kwargs))

        records = [
            json.loads(line.split("litellm_route_trace ", 1)[1])
            for line in captured.output
        ]
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("deployment_success", record["event"])
        self.assertEqual("deployment-9", record["deployment_id"])
        self.assertEqual("req-success-1", record["request_id"])
        self.assertEqual("public-agent", record["model_group"])
        self.assertEqual(2, record["order"])
        self.assertEqual(
            "model=public-agent / provider=provider_alpha / upstream=openai/default-chat"
            " / host=relay.example / key=team / order=2",
            record["route_key"],
        )

    def test_a_request_without_a_route_traces_no_success(self) -> None:
        """A success with no route behind it names no route."""

        hooks, _ = load_hook_module()
        routing = sys.modules["young_router.proxy.routing"]
        with self.assertNoLogs("young_router.route_trace", level="WARNING"):
            routing._record_deployment_success_for_cooldown(
                {"model": "public-agent", "request_id": "req-no-route"}
            )


if __name__ == "__main__":
    unittest.main()
