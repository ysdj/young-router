from __future__ import annotations

from hook_test_utils import *


class HookResponsesRequestPrepTests(HookTestCase):
    @staticmethod
    def _codex_collaboration_request() -> dict:
        return {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "instructions": "Keep the requested implementation complete and tested.",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "collaboration",
                            "tools": [
                                {"type": "function", "name": "list_agents"},
                                {"type": "function", "name": "interrupt_agent"},
                                {"type": "function", "name": "spawn_agent"},
                            ],
                        }
                    ],
                },
                {"role": "user", "content": "Implement the change."},
            ],
            "tools": [],
        }

    def test_codex_descendant_cleanup_instruction_is_enabled_by_default(self) -> None:
        hooks, _ = load_hook_module()
        original = self._codex_collaboration_request()

        modified = hooks._with_codex_descendant_cleanup_instruction(original)

        self.assertIsNotNone(modified)
        assert modified is not None
        instructions = modified["instructions"]
        self.assertIn("wait for it and incorporate its result", instructions)
        self.assertIn("take ownership or reassign that work", instructions)
        self.assertIn("never drop required work", instructions)
        self.assertIn("code, file, test, or other work required", instructions)
        self.assertIn("deepest-first", instructions)
        self.assertIn("call list_agents again", instructions)
        self.assertIn("never a sibling or ancestor", instructions)
        self.assertIn("root agent owns the entire tree", instructions)
        self.assertIn("Every assistant response without a real tool call terminates", instructions)
        self.assertIn("progress-only response would terminate the turn", instructions)
        self.assertIn("invalidates every earlier list_agents snapshot", instructions)
        self.assertIn("Visible answer text alone is not evidence", instructions)
        self.assertEqual(
            original["instructions"],
            "Keep the requested implementation complete and tested.",
        )

        self.assertIsNone(
            hooks._with_codex_descendant_cleanup_instruction(modified)
        )

    def test_codex_descendant_cleanup_instruction_can_be_disabled(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._CODEX_DESCENDANT_CLEANUP_ENV, "0")

        self.assertIsNone(
            hooks._with_codex_descendant_cleanup_instruction(
                self._codex_collaboration_request()
            )
        )

    def test_codex_descendant_cleanup_requires_management_tools(self) -> None:
        hooks, _ = load_hook_module()
        request = self._codex_collaboration_request()
        request["input"][0]["tools"][0]["tools"] = [
            {"type": "function", "name": "spawn_agent"},
        ]

        self.assertIsNone(
            hooks._with_codex_descendant_cleanup_instruction(request)
        )

    def test_codex_descendant_cleanup_ignores_compaction_and_non_codex(self) -> None:
        hooks, _ = load_hook_module()
        compaction = self._codex_collaboration_request()
        compaction["input"].append({"type": "compaction_trigger"})
        self.assertIsNone(
            hooks._with_codex_descendant_cleanup_instruction(compaction)
        )

        non_codex = self._codex_collaboration_request()
        non_codex.pop("client_metadata")
        self.assertIsNone(
            hooks._with_codex_descendant_cleanup_instruction(non_codex)
        )

    async def test_pre_call_injects_codex_descendant_cleanup_instruction(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        modified = await hook.async_pre_call_deployment_hook(
            self._codex_collaboration_request(),
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertIn(
            hooks._CODEX_DESCENDANT_CLEANUP_MARKER,
            modified["instructions"],
        )

    @staticmethod
    def _as_root_collaboration_request(request: dict) -> dict:
        request["input"].insert(
            1,
            {
                "type": "message",
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": "You are `/root`, the primary agent in a team of agents.",
                    }
                ],
            },
        )
        request["tool_choice"] = "auto"
        return request

    @staticmethod
    def _append_collaboration_call(
        request: dict,
        *,
        call_id: str,
        name: str,
        arguments: str,
        output: str,
    ) -> None:
        request["input"].extend(
            [
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "namespace": "collaboration",
                    "arguments": arguments,
                },
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            ]
        )

    def test_codex_root_with_active_descendant_must_make_a_tool_call(self) -> None:
        hooks, _ = load_hook_module()
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        self._append_collaboration_call(
            request,
            call_id="call-list",
            name="list_agents",
            arguments='{"path_prefix":"/root"}',
            output=json.dumps(
                {
                    "agents": [
                        {"agent_name": "/root", "agent_status": "running"},
                        {
                            "agent_name": "/root/audit",
                            "agent_status": "running",
                        },
                    ]
                }
            ),
        )

        modified = hooks._with_codex_descendant_cleanup_instruction(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tool_choice"], "required")
        self.assertEqual(
            modified["litellm_metadata"][hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY]["state"],
            "active_descendants",
        )

    def test_codex_root_without_snapshot_must_list_before_completion(self) -> None:
        hooks, _ = load_hook_module()
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )

        modified = hooks._with_codex_descendant_cleanup_instruction(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tool_choice"], "required")
        self.assertEqual(
            modified["litellm_metadata"][hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY]["state"],
            "snapshot_missing",
        )

    def test_codex_root_clean_snapshot_allows_tool_free_completion(self) -> None:
        hooks, _ = load_hook_module()
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        request["instructions"] = hooks._CODEX_DESCENDANT_CLEANUP_INSTRUCTION
        self._append_collaboration_call(
            request,
            call_id="call-list",
            name="list_agents",
            arguments="{}",
            output=json.dumps(
                {
                    "agents": [
                        {"agent_name": "/root", "agent_status": "running"},
                        {
                            "agent_name": "/root/audit",
                            "agent_status": {"completed": "done"},
                        },
                    ]
                }
            ),
        )

        self.assertIsNone(
            hooks._with_codex_descendant_cleanup_instruction(request)
        )
        self.assertEqual(request["tool_choice"], "auto")

    def test_codex_root_lifecycle_call_invalidates_clean_snapshot(self) -> None:
        hooks, _ = load_hook_module()
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        request["instructions"] = hooks._CODEX_DESCENDANT_CLEANUP_INSTRUCTION
        self._append_collaboration_call(
            request,
            call_id="call-list",
            name="list_agents",
            arguments='{"path_prefix":"/root"}',
            output=json.dumps(
                {"agents": [{"agent_name": "/root", "agent_status": "running"}]}
            ),
        )
        self._append_collaboration_call(
            request,
            call_id="call-followup",
            name="followup_task",
            arguments='{"target":"/root/audit","message":"finish"}',
            output="",
        )

        modified = hooks._with_codex_descendant_cleanup_instruction(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tool_choice"], "required")
        self.assertEqual(
            modified["litellm_metadata"][hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY]["state"],
            "snapshot_invalidated",
        )

    def test_codex_root_full_snapshot_after_followup_releases_barrier(self) -> None:
        hooks, _ = load_hook_module()
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        request["instructions"] = hooks._CODEX_DESCENDANT_CLEANUP_INSTRUCTION
        self._append_collaboration_call(
            request,
            call_id="call-followup",
            name="followup_task",
            arguments='{"target":"/root/audit","message":"finish"}',
            output="",
        )
        self._append_collaboration_call(
            request,
            call_id="call-list",
            name="list_agents",
            arguments='{"path_prefix":"/root"}',
            output=json.dumps(
                {
                    "agents": [
                        {"agent_name": "/root", "agent_status": "running"},
                        {
                            "agent_name": "/root/audit",
                            "agent_status": "completed",
                        },
                    ]
                }
            ),
        )

        self.assertIsNone(
            hooks._with_codex_descendant_cleanup_instruction(request)
        )
        self.assertEqual(request["tool_choice"], "auto")

    def test_codex_root_narrow_snapshot_does_not_release_barrier(self) -> None:
        hooks, _ = load_hook_module()
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        request["instructions"] = hooks._CODEX_DESCENDANT_CLEANUP_INSTRUCTION
        self._append_collaboration_call(
            request,
            call_id="call-spawn",
            name="spawn_agent",
            arguments='{"task_name":"audit","message":"check"}',
            output='{"agent_name":"/root/audit"}',
        )
        self._append_collaboration_call(
            request,
            call_id="call-list",
            name="list_agents",
            arguments='{"path_prefix":"/root/audit"}',
            output=json.dumps(
                {
                    "agents": [
                        {
                            "agent_name": "/root/audit",
                            "agent_status": "completed",
                        }
                    ]
                }
            ),
        )

        modified = hooks._with_codex_descendant_cleanup_instruction(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tool_choice"], "required")

    def test_codex_root_snapshot_called_before_parallel_followup_stays_invalid(self) -> None:
        hooks, _ = load_hook_module()
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        request["instructions"] = hooks._CODEX_DESCENDANT_CLEANUP_INSTRUCTION
        request["input"].extend(
            [
                {
                    "type": "function_call",
                    "call_id": "call-list",
                    "name": "list_agents",
                    "namespace": "collaboration",
                    "arguments": '{"path_prefix":"/root"}',
                },
                {
                    "type": "function_call",
                    "call_id": "call-followup",
                    "name": "followup_task",
                    "namespace": "collaboration",
                    "arguments": '{"target":"/root/audit","message":"finish"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-list",
                    "output": json.dumps(
                        {
                            "agents": [
                                {"agent_name": "/root", "agent_status": "running"}
                            ]
                        }
                    ),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-followup",
                    "output": "",
                },
            ]
        )

        modified = hooks._with_codex_descendant_cleanup_instruction(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tool_choice"], "required")
        self.assertEqual(
            modified["litellm_metadata"][hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY]["state"],
            "snapshot_invalidated",
        )

    def test_codex_root_snapshot_called_after_parallel_followup_stays_invalid(self) -> None:
        hooks, _ = load_hook_module()
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        request["instructions"] = hooks._CODEX_DESCENDANT_CLEANUP_INSTRUCTION
        request["input"].extend(
            [
                {
                    "type": "function_call",
                    "call_id": "call-followup",
                    "name": "followup_task",
                    "namespace": "collaboration",
                    "arguments": '{"target":"/root/audit","message":"finish"}',
                },
                {
                    "type": "function_call",
                    "call_id": "call-list",
                    "name": "list_agents",
                    "namespace": "collaboration",
                    "arguments": '{"path_prefix":"/root"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-followup",
                    "output": "",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-list",
                    "output": json.dumps(
                        {
                            "agents": [
                                {"agent_name": "/root", "agent_status": "running"}
                            ]
                        }
                    ),
                },
            ]
        )

        modified = hooks._with_codex_descendant_cleanup_instruction(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tool_choice"], "required")
        self.assertEqual(
            modified["litellm_metadata"][hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY]["state"],
            "snapshot_invalidated",
        )

    def test_codex_root_progress_text_does_not_split_parallel_batch(self) -> None:
        hooks, _ = load_hook_module()
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        request["instructions"] = hooks._CODEX_DESCENDANT_CLEANUP_INSTRUCTION
        request["input"].extend(
            [
                {
                    "type": "function_call",
                    "call_id": "call-followup",
                    "name": "followup_task",
                    "namespace": "collaboration",
                    "arguments": '{"target":"/root/audit","message":"finish"}',
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Checking descendants.",
                },
                {
                    "type": "function_call",
                    "call_id": "call-list",
                    "name": "list_agents",
                    "namespace": "collaboration",
                    "arguments": '{}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-followup",
                    "output": "",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-list",
                    "output": json.dumps(
                        {"agents": [{"agent_name": "/root", "agent_status": "running"}]}
                    ),
                },
            ]
        )

        modified = hooks._with_codex_descendant_cleanup_instruction(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            modified["litellm_metadata"][hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY]["state"],
            "snapshot_invalidated",
        )

    def test_codex_root_later_same_turn_snapshot_releases_barrier(self) -> None:
        hooks, _ = load_hook_module()
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        request["instructions"] = hooks._CODEX_DESCENDANT_CLEANUP_INSTRUCTION
        turn_metadata = {"turn_id": "turn-parallel"}
        request["input"].extend(
            [
                {
                    "type": "function_call",
                    "call_id": "call-followup",
                    "name": "followup_task",
                    "namespace": "collaboration",
                    "arguments": '{"target":"/root/audit","message":"finish"}',
                    "internal_chat_message_metadata_passthrough": turn_metadata,
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-followup",
                    "output": "",
                },
                {
                    "type": "function_call",
                    "call_id": "call-list",
                    "name": "list_agents",
                    "namespace": "collaboration",
                    "arguments": "{}",
                    "internal_chat_message_metadata_passthrough": turn_metadata,
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-list",
                    "output": json.dumps(
                        {"agents": [{"agent_name": "/root", "agent_status": "running"}]}
                    ),
                },
            ]
        )

        modified = hooks._with_codex_descendant_cleanup_instruction(request)

        self.assertIsNone(modified)
        self.assertEqual(request["tool_choice"], "auto")

    def test_codex_subagent_is_not_subject_to_root_completion_barrier(self) -> None:
        hooks, _ = load_hook_module()
        request = self._codex_collaboration_request()
        request["instructions"] = hooks._CODEX_DESCENDANT_CLEANUP_INSTRUCTION
        request["input"].insert(
            1,
            {
                "role": "developer",
                "content": "You are `/root/audit`, a subagent.",
            },
        )
        request["tool_choice"] = "auto"
        self._append_collaboration_call(
            request,
            call_id="call-list",
            name="list_agents",
            arguments="{}",
            output=json.dumps(
                {
                    "agents": [
                        {"agent_name": "/root", "agent_status": "running"},
                        {
                            "agent_name": "/root/audit",
                            "agent_status": "running",
                        },
                    ]
                }
            ),
        )

        self.assertIsNone(
            hooks._with_codex_descendant_cleanup_instruction(request)
        )
        self.assertEqual(request["tool_choice"], "auto")

    def test_codex_subagent_inheriting_root_history_is_not_root(self) -> None:
        hooks, _ = load_hook_module()
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        request["instructions"] = hooks._CODEX_DESCENDANT_CLEANUP_INSTRUCTION
        request["input"].insert(
            2,
            {
                "role": "developer",
                "content": (
                    "You are an agent in a team of agents collaborating to "
                    "complete a task."
                ),
            },
        )

        self.assertFalse(hooks._codex_request_is_root_agent(request))
        self.assertIsNone(
            hooks._with_codex_descendant_cleanup_instruction(request)
        )
        self.assertEqual(request["tool_choice"], "auto")

    async def test_pre_call_preserves_stale_wait_output(self) -> None:
        hooks, _ = load_hook_module()
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {"type": "custom", "name": "exec"},
                        {"type": "function", "name": "wait"},
                        {"type": "function", "name": "request_user_input"},
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call-wait",
                    "name": "wait",
                    "arguments": '{"cell_id":"expired-cell"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-wait",
                    "output": "Script error: exec cell expired-cell not found",
                },
            ],
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }

        hook = hooks.LiteLLMMenuHook()
        modified = await hook.async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["input"][-1], original["input"][-1])
        self.assertEqual(
            [tool["name"] for tool in modified["tools"]],
            ["exec", "wait", "request_user_input"],
        )
        self.assertNotIn("additional_tools", json.dumps(modified["input"]))

    async def test_pre_call_preserves_unavailable_request_user_input_history(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {"type": "custom", "name": "exec"},
                        {"type": "function", "name": "wait"},
                        {"type": "function", "name": "request_user_input"},
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call-question",
                    "name": "request_user_input",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-question",
                    "output": "request_user_input is unavailable in Default mode",
                },
            ],
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }

        modified = await hook.async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            [tool["name"] for tool in modified["tools"]],
            ["exec", "wait", "request_user_input"],
        )
        self.assertNotIn("additional_tools", json.dumps(modified["input"]))
        self.assertEqual(modified["input"][-1], original["input"][-1])
        self.assertEqual(modified["tool_choice"], "auto")
        self.assertFalse(modified["parallel_tool_calls"])
        self.assertEqual(
            [tool["name"] for tool in original["input"][0]["tools"]],
            ["exec", "wait", "request_user_input"],
        )

    async def test_pre_call_deployment_hook_adds_compat_provider_browser_headers(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "api_base": "https://headers.example/v1",
            "extra_headers": {"X-Trace": "keep-me"},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        self.assertNotEqual(modified, original)
        assert modified is not None
        headers = modified["extra_headers"]
        self.assertEqual(headers["X-Trace"], "keep-me")
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertEqual(headers["Accept"], "application/json, text/plain, */*")
        self.assertIn("Accept-Language", headers)
        self.assertEqual(original["extra_headers"], {"X-Trace": "keep-me"})

    async def test_pre_call_deployment_hook_forwards_codex_user_agent(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "api_base": "https://headers.example/v1",
            "proxy_server_request": {
                "headers": {
                    "user-agent": "codex-local/1.2.3",
                },
            },
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_headers"]["User-Agent"], "codex-local/1.2.3")

    async def test_pre_call_deployment_hook_forwards_litellm_params_proxy_user_agent(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "api_base": "https://example.com/v1",
            "extra_headers": {"X-Trace": "keep-me"},
            "litellm_params": {
                "proxy_server_request": {
                    "headers": {
                        "User-Agent": "LiteLLM%20Menu/1 CFNetwork/3860.600.21 Darwin/25.5.0",
                    },
                },
            },
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_headers"]["X-Trace"], "keep-me")
        self.assertEqual(
            modified["extra_headers"]["User-Agent"],
            "LiteLLM%20Menu/1 CFNetwork/3860.600.21 Darwin/25.5.0",
        )
        self.assertNotIn("Accept", modified["extra_headers"])
        self.assertEqual(original["extra_headers"], {"X-Trace": "keep-me"})

    async def test_pre_call_deployment_hook_codex_user_agent_overrides_old_extra_header(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "api_base": "https://headers.example/v1",
            "extra_headers": {"user-agent": "Mozilla/5.0 stale"},
            "proxy_server_request": {
                "headers": {
                    "User-Agent": "codex-local/9.9.9",
                },
            },
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_headers"]["user-agent"], "codex-local/9.9.9")
        self.assertNotIn("User-Agent", modified["extra_headers"])

    async def test_pre_call_deployment_hook_preserves_existing_user_agent(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "metadata": {"api_base": "https://api.headers.example/v1"},
            "extra_headers": {"user-agent": "custom-client"},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_headers"]["user-agent"], "custom-client")
        self.assertNotIn("User-Agent", modified["extra_headers"])
        self.assertEqual(modified["extra_headers"]["Accept"], "application/json, text/plain, */*")
        self.assertNotIn("metadata", modified)
        self.assertEqual(modified["litellm_metadata"]["api_base"], "https://api.headers.example/v1")

    async def test_pre_call_deployment_hook_reads_compat_provider_api_base_from_litellm_params(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "litellm_params": {
                "api_base": "https://headers.example/v1",
                "proxy_server_request": {
                    "headers": {
                        "user-agent": "codex-local/4.5.6",
                    },
                },
            },
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_headers"]["User-Agent"], "codex-local/4.5.6")
        self.assertEqual(modified["extra_headers"]["Accept"], "application/json, text/plain, */*")

    async def test_pre_call_deployment_hook_moves_metadata_internal_by_default(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "metadata": {"trace_id": "client-trace", "api_base": "https://example.com/v1"},
            "litellm_metadata": {"model_group": "default-chat"},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertNotIn("metadata", modified)
        self.assertEqual(modified["litellm_metadata"]["trace_id"], "client-trace")
        self.assertEqual(modified["litellm_metadata"]["api_base"], "https://example.com/v1")
        self.assertEqual(modified["litellm_metadata"]["model_group"], "default-chat")
        self.assertEqual(original["litellm_metadata"], {"model_group": "default-chat"})

    async def test_pre_call_deployment_hook_preserves_responses_client_metadata_upstream(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        client_metadata = {
            "thread_id": "thread-test-0001",
            "x-codex-turn-metadata": '{"request_kind":"compaction"}',
        }
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": "Create a compact handoff summary for resuming this Codex session.",
                }
            ],
            "client_metadata": client_metadata,
            "prompt_cache_key": "thread-test-0001",
            "extra_body": {"keep": True},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type="aresponses")

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_body"]["client_metadata"], client_metadata)
        self.assertTrue(modified["extra_body"]["keep"])
        self.assertEqual(
            modified["prompt_cache_key"],
            "thread-test-0001",
        )
        self.assertEqual(original["extra_body"], {"keep": True})
        self.assertEqual(original.get("client_metadata"), client_metadata)

    async def test_pre_call_deployment_hook_preserves_codex_compaction_headers_upstream(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        turn_metadata = (
            '{"session_id":"thread-test-0001",'
            '"thread_id":"thread-test-0001",'
            '"request_kind":"compaction"}'
        )
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": "Create a compact handoff summary for resuming this Codex session.",
                }
            ],
            "stream": True,
            "client_metadata": {
                "session_id": "thread-test-0001",
                "thread_id": "thread-test-0001",
                "x-codex-turn-metadata": turn_metadata,
                "x-codex-window-id": "thread-test-0001:7",
            },
            "proxy_server_request": {
                "headers": {
                    "accept": "text/event-stream",
                    "originator": "Codex Desktop",
                    "session-id": "thread-test-0001",
                    "thread-id": "thread-test-0001",
                    "user-agent": "Codex Desktop/0.142.3",
                    "x-client-request-id": "thread-test-0001",
                    "x-codex-beta-features": "remote_compaction_v2",
                    "x-codex-turn-metadata": turn_metadata,
                    "x-codex-window-id": "thread-test-0001:7",
                    "x-openai-internal-codex-responses-lite": "true",
                }
            },
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type="aresponses")

        self.assertIsNotNone(modified)
        assert modified is not None
        headers = {key.lower(): value for key, value in modified["extra_headers"].items()}
        self.assertEqual(headers["accept"], "text/event-stream")
        self.assertEqual(headers["originator"], "Codex Desktop")
        self.assertEqual(headers["session-id"], "thread-test-0001")
        self.assertEqual(headers["thread-id"], "thread-test-0001")
        self.assertEqual(headers["user-agent"], "Codex Desktop/0.142.3")
        self.assertEqual(headers["x-client-request-id"], "thread-test-0001")
        self.assertEqual(headers["x-codex-beta-features"], "remote_compaction_v2")
        self.assertEqual(headers["x-codex-turn-metadata"], turn_metadata)
        self.assertEqual(headers["accept-encoding"], "identity")
        self.assertEqual(
            headers["x-openai-internal-codex-responses-lite"],
            "true",
        )
        self.assertEqual(
            headers["x-codex-window-id"],
            "thread-test-0001:7",
        )

    async def test_pre_call_deployment_hook_does_not_add_responses_client_metadata_to_chat_bridge(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "hi",
            "use_chat_completions_api": True,
            "client_metadata": {"thread_id": "thread"},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type="aresponses")

        if modified is not None:
            self.assertNotIn("extra_body", modified)

    async def test_pre_call_deployment_hook_preserves_codex_compaction_history_byte_for_byte(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        previous_compaction = {
            "type": "compaction",
            "id": "compaction_previous",
            "encrypted_content": "opaque-encrypted-compaction",
        }
        encrypted_agent_message = {
            "type": "agent_message",
            "id": "agent_previous",
            "content": [{"type": "output_text", "text": "results consumed"}],
            "encrypted_content": "opaque-encrypted-agent-message",
        }
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "client_metadata": {
                "thread_id": "thread-preflight-compaction",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "input": [
                {
                    "type": "message",
                    "id": "msg_keep",
                    "role": "developer",
                    "content": "d" * 40_000,
                },
                {
                    "type": "custom_tool_call_output",
                    "id": "out_keep",
                    "call_id": "call_keep",
                    "output": "x" * 600_000,
                },
                previous_compaction,
                encrypted_agent_message,
                {"type": "compaction_trigger", "id": "trigger_keep"},
            ],
        }

        modified = await hook.async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["input"], original["input"])
        self.assertEqual(
            modified["input"][2]["encrypted_content"],
            "opaque-encrypted-compaction",
        )
        self.assertEqual(
            modified["input"][3]["encrypted_content"],
            "opaque-encrypted-agent-message",
        )
        self.assertEqual(modified["input"][1]["output"], "x" * 600_000)

    async def test_pre_call_deployment_hook_keeps_ordinary_codex_turn_byte_for_byte(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "client_metadata": {
                "thread_id": "thread-test-0002",
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {"type": "message", "role": "user", "content": "continue"},
                {
                    "type": "function_call_output",
                    "call_id": "call_a",
                    "output": "a" * 600_000,
                },
                {
                    "type": "agent_message",
                    "id": "agent_previous",
                    "encrypted_content": "opaque-encrypted-agent-message",
                },
            ],
        }

        modified = await hook.async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["input"], original["input"])

    async def test_pre_call_deployment_hook_restores_plaintext_agent_message_content(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        encrypted_value = "gAAAAABvalidopaqueagentmessage"
        plaintext_value = "补充一项对抗边界：同一轮并行工具调用不能提前放行。"
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": [
                {
                    "type": "agent_message",
                    "id": "agent_encrypted",
                    "content": [
                        {"type": "input_text", "text": "Sender: /root/audit"},
                        {
                            "type": "encrypted_content",
                            "encrypted_content": encrypted_value,
                        },
                    ],
                },
                {
                    "type": "agent_message",
                    "id": "agent_plaintext",
                    "content": [
                        {"type": "input_text", "text": "Sender: /root/audit"},
                        {
                            "type": "encrypted_content",
                            "encrypted_content": plaintext_value,
                        },
                    ],
                },
            ],
        }

        modified = await hook.async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            modified["input"][0]["content"][1],
            original["input"][0]["content"][1],
        )
        self.assertEqual(
            modified["input"][1]["content"][1],
            {"type": "input_text", "text": plaintext_value},
        )
        self.assertEqual(
            original["input"][1]["content"][1],
            {
                "type": "encrypted_content",
                "encrypted_content": plaintext_value,
            },
        )

    async def test_pre_call_deployment_hook_preserves_image_before_encrypted_history(self) -> None:
        import base64
        import io
        import os

        from PIL import Image

        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        image = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        original_data_url = (
            "data:image/jpeg;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        )
        previous_compaction = {
            "type": "compaction",
            "id": "compaction_previous",
            "encrypted_content": "opaque-encrypted-compaction",
        }
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "client_metadata": {
                "thread_id": "thread-image-compaction",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "input": [
                previous_compaction,
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_image",
                    "output": [
                        {"type": "input_text", "text": "screenshot"},
                        {"type": "input_image", "image_url": original_data_url},
                    ],
                },
                {
                    "type": "agent_message",
                    "id": "agent_previous",
                    "encrypted_content": "opaque-encrypted-agent-message",
                },
                {"type": "compaction_trigger", "id": "trigger_keep"},
            ],
        }

        modified = await hook.async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["input"][0], previous_compaction)
        self.assertEqual(modified["input"][1]["output"][0], original["input"][1]["output"][0])
        self.assertEqual(
            modified["input"][1]["output"][1]["image_url"],
            original_data_url,
        )
        self.assertEqual(modified["input"][2], original["input"][2])
        self.assertEqual(modified["input"][3], original["input"][3])
        self.assertEqual(
            original["input"][1]["output"][1]["image_url"],
            original_data_url,
        )

    async def test_pre_call_deployment_hook_compresses_image_after_encrypted_history(self) -> None:
        import base64
        import io
        import os

        from PIL import Image

        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        image = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        original_data_url = (
            "data:image/jpeg;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        )
        previous_compaction = {
            "type": "compaction",
            "id": "compaction_previous",
            "encrypted_content": "opaque-encrypted-compaction",
        }
        encrypted_agent_message = {
            "type": "agent_message",
            "id": "agent_previous",
            "encrypted_content": "opaque-encrypted-agent-message",
        }
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "client_metadata": {
                "thread_id": "thread-image-compaction",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "input": [
                previous_compaction,
                encrypted_agent_message,
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_image",
                    "output": [
                        {"type": "input_text", "text": "screenshot"},
                        {"type": "input_image", "image_url": original_data_url},
                    ],
                },
                {"type": "compaction_trigger", "id": "trigger_keep"},
            ],
        }

        modified = await hook.async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["input"][0], previous_compaction)
        self.assertEqual(modified["input"][1], encrypted_agent_message)
        bounded_image = modified["input"][2]["output"][1]
        self.assertEqual(bounded_image["type"], "input_image")
        self.assertTrue(bounded_image["image_url"].startswith("data:image/"))
        self.assertLess(
            hooks._image_data_url_size(bounded_image["image_url"]),
            hooks._image_data_url_size(original_data_url),
        )
        self.assertLessEqual(
            hooks._image_data_url_size(bounded_image["image_url"]),
            hooks._INLINE_IMAGE_SINGLE_TARGET_BYTES,
        )
        self.assertEqual(
            original["input"][2]["output"][1]["image_url"],
            original_data_url,
        )

    async def test_pre_call_deployment_hook_ignores_other_api_bases(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {"api_base": "https://example.com/v1"}

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNone(modified)

    async def test_pre_call_deployment_hook_uses_browser_header_retry_marker(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "api_base": "https://api.image.example/v1",
            "litellm_metadata": {
                hooks._BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY: True,
            },
            "extra_headers": {"User-Agent": "codex-local/1.2.3"},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertIn("Mozilla/5.0", modified["extra_headers"]["User-Agent"])
        self.assertEqual(
            modified["extra_headers"]["Accept"],
            "application/json, text/plain, */*",
        )
        self.assertEqual(modified["extra_headers"]["Accept-Language"], "en-US,en;q=0.9")

    async def test_responses_api_does_not_prefer_browser_compatible_deployments(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {"api_base": "https://api.primary.example/v1"},
                "model_info": {"id": "primary_provider", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {"api_base": "https://headers.example/v1"},
                "model_info": {"id": "compat_provider-normal", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {"api_base": "https://api.backup.example/v1"},
                "model_info": {"id": "backup_provider", "supports_responses_image_generation_tool": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={"original_generic_function": aresponses},
        )

        self.assertEqual(filtered, deployments)

    async def test_generic_response_wrapper_marks_balance_error_without_403_for_failover(self) -> None:
        hooks, _ = load_hook_module()

        class UpstreamBalanceError(Exception):
            status_code = 400

        error = UpstreamBalanceError('{"code":"INSUFFICIENT_BALANCE"}')

        async def original_generic_function(**kwargs):
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(UpstreamBalanceError):
            await request_kwargs["original_generic_function"](
                model="default-chat",
                litellm_metadata={"model_info": {"id": "empty-account"}},
            )

        self.assertEqual(error.failed_deployment_id, "empty-account")
        self.assertEqual(error.num_retries, 0)

    async def test_generic_response_wrapper_marks_temporary_500_for_failover(self) -> None:
        hooks, _ = load_hook_module()

        class UpstreamServerError(Exception):
            status_code = 500

        error = UpstreamServerError("temporary upstream outage")

        async def original_generic_function(**kwargs):
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(UpstreamServerError):
            await request_kwargs["original_generic_function"](
                model="default-chat",
                model_info={"id": "temporary-failure"},
            )

        self.assertEqual(error.failed_deployment_id, "temporary-failure")
        self.assertEqual(request_kwargs["_excluded_deployment_ids"], ["temporary-failure"])
        self.assertEqual(error.num_retries, 0)

    async def test_generic_response_wrapper_advances_capacity_error_when_default_budget_is_zero(self) -> None:
        hooks, _ = load_hook_module()

        class UpstreamCapacityError(Exception):
            pass

        error = UpstreamCapacityError(
            "Selected model is at capacity. Please try a different model."
        )

        async def original_generic_function(**kwargs):
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(UpstreamCapacityError):
            await request_kwargs["original_generic_function"](
                model="default-chat",
                model_info={"id": "capacity-full-deployment"},
        )

        self.assertEqual(error.failed_deployment_id, "capacity-full-deployment")
        self.assertEqual(
            request_kwargs["_excluded_deployment_ids"],
            ["capacity-full-deployment"],
        )
        self.assertEqual(error.excluded_deployment_ids, ["capacity-full-deployment"])
        self.assertFalse(hooks._should_retry_same_deployment_before_fallback(error))
        self.assertEqual(error.num_retries, 0)

    async def test_generic_response_wrapper_retries_responses_404_via_chat_bridge(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class ResponsesNotFound(Exception):
            status_code = 404

        error = ResponsesNotFound('OpenAIException - {"detail":"Not Found"}')

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise error
            return {"output_text": "ok"}

        request_kwargs = {
            "original_generic_function": original_generic_function,
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "hi",
        }
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="hi",
            model_info={"id": "chat-only-route"},
            litellm_metadata={"model_group": "balanced-chat"},
        )

        self.assertEqual(response, {"output_text": "ok"})
        self.assertEqual(len(calls), 2)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertTrue(calls[1]["use_chat_completions_api"])
        self.assertTrue(
            calls[1]["litellm_metadata"][hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY]
        )
        self.assertEqual(calls[1]["model_info"], {"id": "chat-only-route"})
        self.assertFalse(hasattr(error, "failed_deployment_id"))
