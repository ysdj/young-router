from __future__ import annotations

import base64
from unittest import mock

from hook_test_utils import *


class HookResponsesRequestPrepTests(HookTestCase):
    def test_dsh_messages_plaintext_placeholder_is_restored(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "call_type": "anthropic_messages",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "encrypted_content",
                            "encrypted_content": "Perform a web search for the query: OpenAI",
                        }
                    ],
                }
            ],
        }

        modified = hooks._with_dsh_messages_plaintext_content_restored(request)

        self.assertIsNotNone(modified)
        self.assertEqual(
            modified["messages"][0]["content"],
            [{"type": "text", "text": "Perform a web search for the query: OpenAI"}],
        )
        self.assertEqual(request["messages"][0]["content"][0]["type"], "encrypted_content")

    def setUp(self) -> None:
        super().setUp()
        # Replay-image reference copies stay inside a per-test directory so a
        # test never reads or writes the real runtime cache.
        self.use_replay_image_cache()

    def test_mcp_auto_approval_is_disabled_by_default(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = {
            "call_type": "responses",
            "input": "Use the MCP tool.",
            "tools": [{"type": "mcp", "server_label": "example", "require_approval": "always"}],
        }

        self.assertIsNone(hooks._with_mcp_auto_approval(request))
        self.assertEqual("always", request["tools"][0]["require_approval"])

    def test_mcp_auto_approval_rewrites_only_responses_mcp_tools(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._MCP_AUTO_APPROVE_ENV, "1")
        request = {
            "call_type": "responses",
            "input": "Use the MCP tool.",
            "tools": [
                {"type": "function", "name": "keep_function"},
                {"type": "mcp", "server_label": "example", "require_approval": "always"},
                {"type": "mcp", "server_label": "already-open", "require_approval": "never"},
                {"type": "mcp", "server_label": "object-policy", "require_approval": {"never": ["read"]}},
            ],
        }

        modified = hooks._with_mcp_auto_approval(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual("always", request["tools"][1]["require_approval"])
        self.assertEqual(
            ["function", "mcp", "mcp", "mcp"],
            [tool["type"] for tool in modified["tools"]],
        )
        self.assertEqual(
            [None, "never", "never", "never"],
            [tool.get("require_approval") for tool in modified["tools"]],
        )
        self.assertIs(modified["tools"][0], request["tools"][0])

    def test_mcp_auto_approval_does_not_touch_chat_completions(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._MCP_AUTO_APPROVE_ENV, "1")
        request = {
            "use_chat_completions_api": True,
            "messages": [{"role": "user", "content": "Use the MCP tool."}],
            "tools": [{"type": "mcp", "server_label": "example", "require_approval": "always"}],
        }

        self.assertIsNone(hooks._with_mcp_auto_approval(request))

    async def test_pre_call_applies_mcp_auto_approval_patch(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._MCP_AUTO_APPROVE_ENV, "1")
        request = {
            "call_type": "aresponses",
            "input": "Use the MCP tool.",
            "tools": [{"type": "mcp", "server_label": "example", "require_approval": "always"}],
        }

        modified = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            request,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual("never", modified["tools"][0]["require_approval"])
        self.assertEqual("always", request["tools"][0]["require_approval"])

    @staticmethod
    def _codex_collaboration_request() -> dict:
        return {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "model_info": {
                "supports_responses_web_search": False,
            },
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

    def test_codex_tool_registry_is_carried_by_the_request_not_a_prompt(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._codex_collaboration_request()

        # The declared set is request data the client already sends; nothing is
        # appended to ``instructions`` to announce it.
        self.assertIsNone(contract.with_tool_registry_contract(request))
        self.assertEqual(
            request["instructions"],
            "Keep the requested implementation complete and tested.",
        )
        self.assertIn("list_agents", contract.declared_tool_names(request))

    def test_codex_undeclared_tool_call_is_a_protocol_error(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._codex_collaboration_request()

        self.assertTrue(contract.is_undeclared_tool_call(request, "web__run"))
        self.assertFalse(contract.is_undeclared_tool_call(request, "list_agents"))
        # A request that declares nothing has no contract to enforce.
        self.assertFalse(
            contract.is_undeclared_tool_call({"input": []}, "web__run")
        )

    def test_codex_undeclared_tool_call_answers_with_a_stated_failure(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module

        event = contract.undeclared_tool_response_event("web__run", "call-1")

        self.assertEqual(event["type"], "response.failed")
        error = event["response"]["error"]
        self.assertEqual(error["code"], contract.UNDECLARED_TOOL_CODE)
        self.assertEqual(error["tool_name"], "web__run")

    def test_generic_additional_tools_do_not_identify_codex(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "call_type": "aresponses",
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "utilities",
                            "tools": [{"type": "function", "name": "lookup"}],
                        }
                    ],
                }
            ],
        }

        self.assertFalse(hooks._responses_request_module._request_has_codex_client_evidence(request))

    def test_codex_gpt_image_family_is_not_a_gpt_responses_route(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        original = self._codex_collaboration_request()
        original["model"] = "gpt-image-2"
        original["model_info"] = {
            "provider": "third-party",
            "upstream_url_surface": "openai/responses",
        }

        # A GPT image model is not a GPT Responses chat route, so the Codex
        # tool registry is never applied to it.
        self.assertFalse(hooks._request_is_codex_gpt_responses_route(original))

    def test_codex_openrouter_hosted_search_is_converted_to_native_declaration(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        original = self._codex_collaboration_request()
        original["tools"] = [
            {"type": "web_search", "search_context_size": "high"},
        ]
        original["web_search_options"] = {"search_context_size": "high"}
        original["tool_choice"] = {"type": "web_search"}
        original["model_info"] = {
            "provider": "openrouter",
            "upstream_url_surface": "openai/responses",
            "supports_responses_function_tools": True,
        }

        modified = hooks._with_codex_openrouter_native_web_search_tool(original)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tools"], [{"type": "openrouter:web_search"}])
        self.assertEqual(modified["tool_choice"], {"type": "openrouter:web_search"})
        self.assertNotIn("web_search_options", modified)
        self.assertEqual(
            original["tools"],
            [{"type": "web_search", "search_context_size": "high"}],
        )
        self.assertIn("web_search_options", original)

    def test_codex_openrouter_nested_native_search_is_promoted_without_duplicate(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        original = self._codex_collaboration_request()
        original["input"][0]["tools"].append(
            {"type": "openrouter:web_search"}
        )
        original["model_info"] = {
            "provider": "openrouter",
            "upstream_url_surface": "openai/responses",
            "supports_responses_function_tools": True,
        }

        modified = hooks._with_codex_openrouter_native_web_search_tool(original)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tools"], [{"type": "openrouter:web_search"}])
        self.assertEqual(
            [tool.get("type") for tool in modified["input"][0]["tools"]],
            ["namespace"],
        )
        self.assertEqual(
            original["input"][0]["tools"][-1],
            {"type": "openrouter:web_search"},
        )

    async def test_codex_openrouter_pre_call_exposes_native_search_with_client_tools(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        original = self._codex_collaboration_request()
        original["model_info"] = {
            "provider": "openrouter",
            "upstream_url_surface": "openai/responses",
            "supports_responses_function_tools": True,
        }

        modified = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertIn(
            {"type": "openrouter:web_search"},
            modified["tools"],
        )
        self.assertFalse(
            any(
                tool.get("name") in {"web_search", "fetch_content"}
                for tool in modified["tools"]
                if isinstance(tool, dict)
            )
        )

    async def test_codex_openrouter_pre_call_injects_native_search_on_plain_turn(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        original = {
            "call_type": "aresponses",
            "model": "openrouter-model",
            "stream": True,
            "model_info": {
                "provider": "openrouter",
                "upstream_url_surface": "openai/responses",
                "supports_responses_function_tools": True,
            },
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [{"role": "user", "content": "Current information?"}],
            "tools": [],
        }

        modified = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tools"], [{"type": "openrouter:web_search"}])

    def test_codex_declared_set_covers_a_function_namespace(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._codex_collaboration_request()
        request["input"][0]["tools"].insert(
            0,
            {
                "type": "namespace",
                "name": "functions",
                "tools": [
                    {"type": "custom", "name": "exec"},
                    {"type": "function", "name": "wait"},
                ],
            },
        )

        declared = contract.declared_tool_names(request)

        self.assertIn("exec", declared)
        self.assertIn("wait", declared)
        self.assertFalse(contract.is_undeclared_tool_call(request, "exec"))
        self.assertTrue(contract.is_undeclared_tool_call(request, "web__run"))

    def test_codex_tool_registry_instruction_ignores_non_codex_requests(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._codex_collaboration_request()
        request.pop("client_metadata")

        self.assertIsNone(contract.with_tool_registry_contract(request))

    async def test_pre_call_never_injects_codex_rule_text(self) -> None:
        hooks, _ = load_hook_module()
        modified = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            self._codex_collaboration_request(),
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        instructions = modified.get("instructions") or ""
        for marker in (
            "<young_router_codex_tool_registry>",
            "<young_router_codex_descendant_cleanup>",
            "<young_router_codex_commentary_discipline>",
        ):
            self.assertNotIn(marker, instructions)
        self.assertEqual(
            {tool.get("name") for tool in modified["tools"] if isinstance(tool, dict)},
            {"collaboration"},
        )

    def test_codex_descendant_barrier_holds_the_turn_with_tool_choice(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
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
                        {"agent_name": "/root/audit", "agent_status": "running"},
                    ]
                }
            ),
        )

        modified = contract.with_descendant_cleanup_barrier(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        # The barrier is the tool choice and the lifecycle state; the caller's
        # own instructions pass through byte-for-byte, so no rule text can be
        # misread as part of the task.
        self.assertEqual(modified["tool_choice"], "required")
        self.assertEqual(
            modified["instructions"],
            request["instructions"],
        )
        self.assertEqual(
            modified["litellm_metadata"][
                hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY
            ]["state"],
            "active_descendants",
        )

    def test_codex_contract_steps_never_write_a_prompt(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )

        for step in contract.CODEX_CONTRACT_REQUEST_STEPS:
            modified = step(request)
            if modified is None:
                continue
            self.assertNotIn("instructions", modified, step.__name__)

    def test_codex_descendant_cleanup_instruction_can_be_disabled(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._CODEX_DESCENDANT_CLEANUP_ENV, "0")

        self.assertIsNone(
            contract.with_descendant_cleanup_barrier(
                self._codex_collaboration_request()
            )
        )

    def test_codex_descendant_cleanup_requires_management_tools(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._codex_collaboration_request()
        request["input"][0]["tools"][0]["tools"] = [
            {"type": "function", "name": "spawn_agent"},
        ]

        self.assertIsNone(
            contract.with_descendant_cleanup_barrier(request)
        )

    def test_codex_descendant_cleanup_ignores_compaction_and_non_codex(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        compaction = self._codex_collaboration_request()
        compaction["input"].append({"type": "compaction_trigger"})
        self.assertIsNone(
            contract.with_descendant_cleanup_barrier(compaction)
        )

        non_codex = self._codex_collaboration_request()
        non_codex.pop("client_metadata")
        self.assertIsNone(
            contract.with_descendant_cleanup_barrier(non_codex)
        )

    async def test_pre_call_applies_the_barrier_without_rule_text(self) -> None:
        hooks, _ = load_hook_module()
        request = self._codex_collaboration_request()
        request["input"].append(
            {"role": "assistant", "content": [{"type": "output_text", "text": "I will."}]}
        )
        hook = hooks.YoungRouterHook()

        modified = await hook.async_pre_call_deployment_hook(
            request,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tool_choice"], "required")
        self.assertNotIn("<young_router_codex_descendant_cleanup>", modified.get("instructions") or "")

    def test_codex_progress_message_contract_holds_the_turn_open(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        original = self._codex_collaboration_request()
        request = json.loads(json.dumps(original))
        request["input"].append(
            {
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "I will now run the tests."}
                ],
            }
        )

        modified = contract.with_progress_message_contract(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tool_choice"], "required")
        self.assertEqual(
            modified["instructions"],
            original["instructions"],
        )

    def test_codex_commentary_discipline_instruction_can_be_disabled(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._CODEX_COMMENTARY_DISCIPLINE_ENV, "0")

        self.assertIsNone(
            contract.with_progress_message_contract(
                self._codex_collaboration_request()
            )
        )

    def test_codex_commentary_discipline_needs_a_declared_tool(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._codex_collaboration_request()
        request["input"][0]["tools"] = []

        self.assertIsNone(
            contract.with_progress_message_contract(request)
        )

    def test_codex_commentary_discipline_ignores_compaction_and_non_codex(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        compaction = self._codex_collaboration_request()
        compaction["input"].append({"type": "compaction_trigger"})
        self.assertIsNone(
            contract.with_progress_message_contract(compaction)
        )

        non_codex = self._codex_collaboration_request()
        non_codex.pop("client_metadata")
        self.assertIsNone(
            contract.with_progress_message_contract(non_codex)
        )

    async def test_pre_call_never_writes_a_rule_into_instructions(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        modified = await hook.async_pre_call_deployment_hook(
            self._codex_collaboration_request(),
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        instructions = modified.get("instructions") or ""
        for marker in (
            "<young_router_codex_tool_registry>",
            "<young_router_codex_descendant_cleanup>",
            "<young_router_codex_commentary_discipline>",
        ):
            self.assertNotIn(marker, instructions)

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
        contract = hooks._codex_contract_module
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

        modified = contract.with_descendant_cleanup_barrier(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["tool_choice"], "required")
        self.assertEqual(
            modified["litellm_metadata"][hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY]["state"],
            "active_descendants",
        )

    def test_codex_root_without_descendant_history_keeps_requested_choice(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )

        # A root that has never run a descendant-management call holds no
        # barrier at all, so its own choice survives untouched.
        self.assertIsNone(
            hooks._codex_descendant_cleanup_runtime_state(request)
        )
        self.assertIsNone(contract.with_descendant_cleanup_barrier(request))
        self.assertEqual(request["tool_choice"], "auto")

    def test_codex_cleanup_does_not_reinject_forced_choice_after_protocol_relaxation(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        # A lifecycle call with no snapshot after it is a real invalidated
        # state, which is the state this relaxation rule exists for.
        self._append_collaboration_call(
            request,
            call_id="call-spawn",
            name="spawn_agent",
            arguments='{"task_name":"audit","message":"check"}',
            output='{"agent_name":"/root/audit"}',
        )
        request["litellm_metadata"] = {
            hooks._PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY: True,
        }

        modified = contract.with_descendant_cleanup_barrier(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        # The state is recorded, but the refused named choice is not re-injected.
        self.assertEqual(
            modified["litellm_metadata"][
                hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY
            ]["state"],
            "snapshot_invalidated",
        )
        self.assertNotEqual(
            modified.get("tool_choice"),
            {"type": "function", "name": "list_agents"},
        )

    def test_codex_root_invalidated_snapshot_preserves_existing_required_choice(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
        self._append_collaboration_call(
            request,
            call_id="call-spawn",
            name="spawn_agent",
            arguments='{"task_name":"audit","message":"check"}',
            output='{"agent_name":"/root/audit"}',
        )
        request["tool_choice"] = "required"

        modified = contract.with_descendant_cleanup_barrier(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        # Snapshot recovery has exactly one valid action, so the barrier names
        # it rather than leaving a generic ``required`` the model can satisfy
        # with an unrelated tool.
        self.assertEqual(
            modified["tool_choice"],
            {"type": "function", "name": "list_agents"},
        )

    def test_codex_declared_set_covers_promoted_top_level_tools(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._codex_collaboration_request()
        request["input"] = [{"role": "user", "content": "Implement the change."}]
        request["tools"] = [
            {"type": "function", "name": "exec"},
            {"type": "function", "name": "wait"},
            {"type": "function", "name": "list_agents"},
        ]

        declared = contract.declared_tool_names(request)

        self.assertEqual(declared, {"exec", "wait", "list_agents"})
        self.assertFalse(contract.is_undeclared_tool_call(request, "exec"))
        self.assertTrue(contract.is_undeclared_tool_call(request, "web__run"))

    def test_codex_declared_set_covers_loaded_browser_tools(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._codex_collaboration_request()
        request["input"].insert(
            1,
            {
                "type": "tool_search_output",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__pi_control_chrome",
                        "tools": [
                            {"type": "function", "name": "browser_targets"},
                            {"type": "function", "name": "browser_snapshot"},
                        ],
                    }
                ],
            },
        )

        declared = contract.declared_tool_names(request)

        self.assertIn("browser_targets", declared)
        self.assertIn("browser_snapshot", declared)
        self.assertFalse(
            contract.is_undeclared_tool_call(request, "browser_snapshot")
        )
        self.assertTrue(contract.is_undeclared_tool_call(request, "browser_missing"))

    def test_codex_declared_set_covers_a_trailing_additional_tools_item(self) -> None:
        """The declared set is every ``additional_tools`` item, not the leading run.

        Codex sends its always-on registry as a leading item and appends the
        deferred registry -- the tools a ``tool_search`` unlocks -- behind the
        conversation it belongs to.  Reading only the leading run declared
        neither ``view_image`` nor ``exec_command``, so the guard answered each
        of their calls with ``response.failed`` ("which this request never
        declared") even though the bridge forwarded them upstream and the
        client could run them.  The bridge reads every item, so the contract
        that judges the answer has to read the same set.
        """

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._codex_collaboration_request()
        request["input"].extend(
            [
                {"role": "user", "content": "Re-run the failing check."},
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "functions",
                            "tools": [
                                {"type": "function", "name": "view_image"},
                                {"type": "function", "name": "exec_command"},
                            ],
                        }
                    ],
                },
            ]
        )

        declared = contract.declared_tool_names(request)

        self.assertIn("view_image", declared)
        self.assertIn("exec_command", declared)
        # The leading registry still counts.
        self.assertIn("list_agents", declared)
        self.assertFalse(contract.is_undeclared_tool_call(request, "view_image"))
        self.assertTrue(contract.is_undeclared_tool_call(request, "browser_missing"))
        # The trace counts the same set, so a rejection is readable afterwards.
        summary = hooks._trace_request_summary(request)["tools"]
        self.assertIn("view_image", summary["names"])
        self.assertIn("additional_tools", summary["origins"])

    def test_codex_root_clean_snapshot_allows_tool_free_completion(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
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
            contract.with_descendant_cleanup_barrier(request)
        )
        self.assertEqual(request["tool_choice"], "auto")

    def test_codex_root_lifecycle_call_invalidates_clean_snapshot(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
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

        modified = contract.with_descendant_cleanup_barrier(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            modified["tool_choice"],
            {"type": "function", "name": "list_agents"},
        )
        self.assertEqual(
            modified["litellm_metadata"][hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY]["state"],
            "snapshot_invalidated",
        )

    def test_codex_root_full_snapshot_after_followup_releases_barrier(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
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
            contract.with_descendant_cleanup_barrier(request)
        )
        self.assertEqual(request["tool_choice"], "auto")

    def test_codex_root_narrow_snapshot_does_not_release_barrier(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
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

        modified = contract.with_descendant_cleanup_barrier(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            modified["tool_choice"],
            {"type": "function", "name": "list_agents"},
        )

    def test_codex_root_snapshot_called_before_parallel_followup_stays_invalid(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
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

        modified = contract.with_descendant_cleanup_barrier(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            modified["tool_choice"],
            {"type": "function", "name": "list_agents"},
        )
        self.assertEqual(
            modified["litellm_metadata"][hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY]["state"],
            "snapshot_invalidated",
        )

    def test_codex_root_snapshot_called_after_parallel_followup_stays_invalid(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
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

        modified = contract.with_descendant_cleanup_barrier(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            modified["tool_choice"],
            {"type": "function", "name": "list_agents"},
        )
        self.assertEqual(
            modified["litellm_metadata"][hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY]["state"],
            "snapshot_invalidated",
        )

    def test_codex_root_progress_text_does_not_split_parallel_batch(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
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

        modified = contract.with_descendant_cleanup_barrier(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            modified["litellm_metadata"][hooks._CODEX_DESCENDANT_CLEANUP_METADATA_KEY]["state"],
            "snapshot_invalidated",
        )

    def test_codex_root_later_same_turn_snapshot_releases_barrier(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
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

        modified = contract.with_descendant_cleanup_barrier(request)

        self.assertIsNone(modified)
        self.assertEqual(request["tool_choice"], "auto")

    def test_codex_subagent_is_not_subject_to_root_completion_barrier(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._codex_collaboration_request()
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
            contract.with_descendant_cleanup_barrier(request)
        )
        self.assertEqual(request["tool_choice"], "auto")

    def test_codex_subagent_inheriting_root_history_is_not_root(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = self._as_root_collaboration_request(
            self._codex_collaboration_request()
        )
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
            contract.with_descendant_cleanup_barrier(request)
        )
        self.assertEqual(request["tool_choice"], "auto")

    async def test_pre_call_preserves_stale_wait_output(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "model_info": {
                "supports_responses_web_search": False,
            },
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

        hook = hooks.YoungRouterHook()
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

    async def test_pre_call_flattens_text_only_wait_output_after_encrypted_history(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        encrypted_reasoning = {
            "type": "reasoning",
            "encrypted_content": "opaque-encrypted-reasoning",
        }
        prior_output = {
            "type": "function_call_output",
            "call_id": "call-prior",
            "output": [{"type": "input_text", "text": "signed prefix"}],
        }
        current_output = {
            "type": "function_call_output",
            "call_id": "call-wait",
            "output": [
                {"type": "input_text", "text": "Script completed\n"},
                {"type": "input_text", "text": "first draft ready\n"},
                {"type": "input_text", "text": "second draft ready\n"},
            ],
        }
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "model_info": {
                "supports_responses_web_search": False,
            },
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                prior_output,
                encrypted_reasoning,
                {
                    "type": "function_call",
                    "call_id": "call-wait",
                    "name": "wait",
                    "arguments": '{"cell_id":"running-cell"}',
                },
                current_output,
            ],
            "tools": [{"type": "function", "name": "wait"}],
            "tool_choice": "auto",
        }

        modified = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertIs(modified["input"][0], prior_output)
        self.assertEqual(modified["input"][0]["output"], prior_output["output"])
        self.assertIs(modified["input"][1], encrypted_reasoning)
        self.assertEqual(
            modified["input"][3]["output"],
            "Script completed\n\nfirst draft ready\n\nsecond draft ready\n",
        )
        self.assertEqual(current_output["output"][0]["type"], "input_text")

    def test_codex_function_output_patch_preserves_non_text_parts(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        original = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "call-image",
                    "output": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,abc",
                        }
                    ],
                }
            ],
        }

        self.assertIsNone(hooks._with_codex_function_call_output_text(original))

    async def test_pre_call_repairs_stream_placeholder_arguments_only_in_mutable_suffix(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        signed_call = {
            "type": "function_call",
            "call_id": "call-signed",
            "name": "exec_command",
            "arguments": '{}{"cmd":"printf signed"}',
        }
        encrypted_reasoning = {
            "type": "reasoning",
            "encrypted_content": "opaque-encrypted-reasoning",
        }
        mutable_call = {
            "type": "function_call",
            "call_id": "call-current",
            "name": "exec_command",
            "arguments": '{}{"cmd":"printf current"}',
        }
        invalid_call = {
            "type": "function_call",
            "call_id": "call-invalid",
            "name": "exec_command",
            "arguments": "{}not-json",
        }
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [signed_call, encrypted_reasoning, mutable_call, invalid_call],
        }

        modified = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertIs(modified["input"][0], signed_call)
        self.assertEqual(modified["input"][0]["arguments"], signed_call["arguments"])
        self.assertIs(modified["input"][1], encrypted_reasoning)
        self.assertEqual(modified["input"][2]["arguments"], '{"cmd":"printf current"}')
        self.assertEqual(modified["input"][3]["arguments"], "{}not-json")
        self.assertEqual(mutable_call["arguments"], '{}{"cmd":"printf current"}')

    def test_codex_view_image_output_keeps_reopenable_paths(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        paths = [
            "/Users/example/project/qa/first.png",
            "/Users/example/project/qa/second.png",
        ]
        original = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call-images",
                    "name": "exec",
                    "input": (
                        "const paths = [\"/Users/example/project/qa/first.png\","
                        " \"/Users/example/project/qa/second.png\"];"
                        " const results = await Promise.all(paths.map(path => "
                        "tools.view_image({path, detail:\"high\"})));"
                        " for (const result of results) image(result.image_url);"
                    ),
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-images",
                    "output": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,abc",
                        },
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,def",
                        },
                    ],
                },
            ],
        }

        modified = hooks._with_codex_view_image_output_paths(original)

        self.assertIsNotNone(modified)
        assert modified is not None
        output = modified["input"][1]["output"]
        self.assertEqual(output[1:], original["input"][1]["output"])
        self.assertIn(hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER, output[0]["text"])
        for path in paths:
            self.assertIn(path, output[0]["text"])
        self.assertEqual(original["input"][1]["output"][0]["image_url"], "data:image/png;base64,abc")

    def test_codex_view_image_path_patch_skips_signed_prefix(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        signed_output = {
            "type": "custom_tool_call_output",
            "call_id": "call-signed",
            "output": [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,signed-bytes",
                }
            ],
        }
        signed_call = {
            "type": "custom_tool_call",
            "call_id": "call-signed",
            "name": "exec",
            "input": 'await tools.view_image({path:"/tmp/signed.png"});',
        }
        encrypted = {
            "type": "reasoning",
            "encrypted_content": "opaque-signed-history",
        }
        mutable_call = {
            "type": "custom_tool_call",
            "call_id": "call-mutable",
            "name": "exec",
            "input": 'await tools.view_image({path:"/tmp/mutable.png"});',
        }
        mutable_output = {
            "type": "custom_tool_call_output",
            "call_id": "call-mutable",
            "output": [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,mutable-bytes",
                }
            ],
        }
        original = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [signed_call, signed_output, encrypted, mutable_call, mutable_output],
        }

        modified = hooks._with_codex_view_image_output_paths(original)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["input"][0], signed_call)
        self.assertEqual(modified["input"][1], signed_output)
        mutable_parts = modified["input"][4]["output"]
        self.assertIn(hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER, mutable_parts[0]["text"])
        self.assertIn("/tmp/mutable.png", mutable_parts[0]["text"])
        self.assertEqual(mutable_parts[1], mutable_output["output"][0])

    def use_replay_image_cache(self) -> Path:
        """Point the durable replay-image cache at a per-test directory."""

        directory = tempfile.TemporaryDirectory(prefix="young-router-replay-images-")
        self.addCleanup(directory.cleanup)
        cache_dir = Path(directory.name)
        self.set_env("YOUNG_ROUTER_IMAGE_CACHE_DIR", str(cache_dir))
        return cache_dir

    @staticmethod
    def reference_paths(text: str) -> list[str]:
        """Return the ``1. /local/path`` reference lines of one marker text."""

        paths: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            number, separator, path = stripped.partition(". ")
            if separator and number.isdigit():
                paths.append(path.strip())
        return paths

    def _prefix_preview_request(self) -> dict:
        oversized_url = "data:image/png;base64," + ("A" * 200_000)
        call = {
            "type": "custom_tool_call",
            "call_id": "call-frozen",
            "name": "exec",
            "input": 'await tools.view_image({path:"/tmp/scan/frozen.png"});',
        }
        output = {
            "type": "custom_tool_call_output",
            "call_id": "call-frozen",
            "id": "ctco_frozen",
            "output": [
                {"type": "input_text", "text": "Script completed"},
                {"type": "input_image", "image_url": oversized_url},
            ],
        }
        encrypted = {
            "type": "reasoning",
            "encrypted_content": "opaque-signed-history",
        }
        return {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [call, output, encrypted],
        }

    def test_prefix_image_previews_replace_oversized_frozen_view_image_outputs(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        cache_dir = self.use_replay_image_cache()
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        original = self._prefix_preview_request()
        frozen_output = original["input"][1]

        resize_calls: list[str] = []

        def fake_resize(value: str, *, target_bytes: int, max_edge: int) -> str:
            resize_calls.append(value)
            return "data:image/jpeg;base64,c2hydW5r"

        with mock.patch.object(
            hooks._image_inputs_module, "_resize_data_url", side_effect=fake_resize
        ):
            hooks._PREFIX_IMAGE_PREVIEW_CACHE.clear()
            modified = hooks._with_prefix_image_previews(original)
            self.assertIsNotNone(modified)
            assert modified is not None
            # Cached second pass does not re-encode.
            modified_again = hooks._with_prefix_image_previews(original)

        self.assertEqual(len(resize_calls), 1)
        self.assertIsNotNone(modified_again)
        assert modified_again is not None

        updated = modified["input"][1]
        self.assertEqual(updated["call_id"], "call-frozen")
        self.assertEqual(updated["id"], "ctco_frozen")
        reference = updated["output"][0]
        self.assertEqual("input_text", reference["type"])
        self.assertIn(hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER, reference["text"])
        # The reference repeats the path the client's own call wrote.  Whether
        # this proxy can see that file is not evidence about the client, so no
        # copy is substituted for it.
        # The reference repeats the path the fixture's call itself named.
        self.assertEqual(
            self.reference_paths(reference["text"]),
            ["/tmp/scan/frozen.png"],
        )
        self.assertEqual(updated["output"][1], frozen_output["output"][0])
        self.assertEqual(
            updated["output"][2]["image_url"], "data:image/jpeg;base64,c2hydW5r"
        )
        # The frozen encrypted item and the call stay byte-identical.
        self.assertEqual(modified["input"][2], original["input"][2])
        self.assertEqual(modified["input"][0], original["input"][0])
        # The client's own history is never mutated.
        self.assertEqual(original["input"][1]["output"][1]["image_url"], frozen_output["output"][1]["image_url"])

    async def test_pre_call_hook_reduces_prefix_images_before_context_selection(self) -> None:
        hooks, _ = load_hook_module()
        self.use_replay_image_cache()
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        original = self._prefix_preview_request()
        original_url = original["input"][1]["output"][1]["image_url"]

        with mock.patch.object(
            hooks._responses_request_module,
            "_prefix_image_preview_resized",
            return_value="data:image/jpeg;base64,pre-call-preview",
        ) as preview:
            modified = await hooks.YoungRouterHook().async_pre_call_hook(
                None,
                None,
                original,
                "aresponses",
            )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            modified["input"][1]["output"][2]["image_url"],
            "data:image/jpeg;base64,pre-call-preview",
        )
        preview.assert_called_once_with(original_url)
        self.assertEqual(
            original["input"][1]["output"][1]["image_url"],
            original_url,
        )

    def test_prefix_image_previews_advertise_the_client_path(self) -> None:
        hooks, _ = load_hook_module()
        cache_dir = self.use_replay_image_cache()
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        request = self._prefix_preview_request()

        with mock.patch.object(
            hooks._responses_request_module,
            "_prefix_image_preview_resized",
            return_value="data:image/jpeg;base64,client-preview",
        ):
            modified = hooks._with_prefix_image_previews(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        output = modified["input"][1]["output"]
        # The reference is prepended, so the client's own text and the bounded
        # preview follow it in their original order.
        reference = output[0]["text"]
        self.assertIn(hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER, reference)
        self.assertEqual(output[1], {"type": "input_text", "text": "Script completed"})
        self.assertEqual(output[2]["image_url"], "data:image/jpeg;base64,client-preview")
        # The reference names the client's own path, so one more view_image call
        # can still reach the original from the machine that runs the tool.
        self.assertIn("/tmp/scan/frozen.png", reference)
        # The advertised path is the client's, never a copy this proxy made.
        self.assertNotIn(str(cache_dir), reference)

    def test_prefix_image_previews_bound_unpaired_images_without_a_reference(self) -> None:
        hooks, _ = load_hook_module()
        self.use_replay_image_cache()
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        request = self._prefix_preview_request()
        request["input"] = request["input"][1:]

        with mock.patch.object(
            hooks._responses_request_module,
            "_prefix_image_preview_resized",
            return_value="data:image/jpeg;base64,unpaired-preview",
        ):
            modified = hooks._with_prefix_image_previews(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            modified["input"][0]["output"][1]["image_url"],
            "data:image/jpeg;base64,unpaired-preview",
        )
        # No path was named by the producing call, so none may be invented.
        self.assertNotIn(
            hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER,
            json.dumps(modified["input"][0]["output"]),
        )

    def test_prefix_image_previews_preview_unpaired_images_without_reference_text(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        oversized_url = "data:image/png;base64," + ("B" * 150_000)
        other_url = "data:image/png;base64," + ("C" * 160_000)
        one_path_call = {
            "type": "custom_tool_call",
            "call_id": "call-multi",
            "name": "exec",
            "input": 'await tools.view_image({path:"/tmp/only-one.png"});',
        }
        multi_output = {
            "type": "custom_tool_call_output",
            "call_id": "call-multi",
            "id": "ctco_multi",
            "output": [
                {"type": "input_text", "text": "Script completed"},
                {"type": "input_image", "image_url": oversized_url},
                {"type": "input_image", "image_url": other_url},
            ],
        }
        original_call = {
            "type": "custom_tool_call",
            "call_id": "call-original",
            "name": "exec",
            "input": 'await tools.view_image({path:"/tmp/orig.png", detail: "original"});',
        }
        original_output = {
            "type": "custom_tool_call_output",
            "call_id": "call-original",
            "id": "ctco_orig",
            "output": [
                {"type": "input_image", "image_url": oversized_url},
            ],
        }
        encrypted = {"type": "reasoning", "encrypted_content": "opaque"}
        request = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                one_path_call,
                multi_output,
                original_call,
                original_output,
                encrypted,
            ],
        }

        seen: list[str] = []

        def fake_resize(value: str, *, target_bytes: int, max_edge: int) -> str:
            seen.append(value)
            return "data:image/jpeg;base64,shrunk"

        hooks._PREFIX_IMAGE_PREVIEW_CACHE.clear()
        with mock.patch.object(
            hooks._image_inputs_module, "_resize_data_url", side_effect=fake_resize
        ):
            modified = hooks._with_prefix_image_previews(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(len(seen), 2)
        # Unpaired images are previewed without a reference part.
        updated_multi = modified["input"][1]
        self.assertEqual(updated_multi["id"], "ctco_multi")
        self.assertEqual(updated_multi["call_id"], "call-multi")
        self.assertEqual(
            [part.get("type") for part in updated_multi["output"]],
            ["input_text", "input_image", "input_image"],
        )
        self.assertEqual(
            [part.get("image_url") for part in updated_multi["output"] if part.get("type") == "input_image"],
            ["data:image/jpeg;base64,shrunk", "data:image/jpeg;base64,shrunk"],
        )
        self.assertNotIn(
            hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER,
            json.dumps(updated_multi["output"]),
        )
        # An explicit original-resolution reopen is replayed at the same bounded
        # preview fidelity as every other history image, together with the
        # matching path reference that keeps the original openable.
        updated_original = modified["input"][3]
        self.assertEqual(updated_original["call_id"], "call-original")
        self.assertEqual(updated_original["id"], "ctco_orig")
        self.assertEqual(
            [part.get("type") for part in updated_original["output"]],
            ["input_text", "input_image"],
        )
        self.assertIn(
            hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER,
            updated_original["output"][0]["text"],
        )
        self.assertEqual(
            updated_original["output"][1]["image_url"],
            "data:image/jpeg;base64,shrunk",
        )
        self.assertEqual(modified["input"][4], encrypted)

        # Disabled by setting -> untouched.
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "0")
        with mock.patch.object(
            hooks._image_inputs_module, "_resize_data_url"
        ) as resize_mock:
            self.assertIsNone(hooks._with_prefix_image_previews(request))
            resize_mock.assert_not_called()

    def test_prefix_image_previews_handle_wire_function_call_shape(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        cache_dir = self.use_replay_image_cache()
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        # 真实线格式：arguments 是 JSON 对象，脚本嵌在 input 字段、引号被转义
        wire_call = {
            "type": "function_call",
            "call_id": "call-wire",
            "name": "exec",
            "arguments": (
                '{"input":"const r = await tools.view_image({path:\\"/tmp/wire.png\\",'
                ' detail:\\"high\\"}); image(r.image_url, \\"high\\");"}'
            ),
        }
        wire_output = {
            "type": "function_call_output",
            "call_id": "call-wire",
            "id": "fco_wire",
            "output": [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + ("W" * 150_000),
                }
            ],
        }
        encrypted = {"type": "reasoning", "encrypted_content": "opaque"}
        request = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [wire_call, wire_output, encrypted],
        }

        hooks._PREFIX_IMAGE_PREVIEW_CACHE.clear()
        with mock.patch.object(
            hooks._image_inputs_module,
            "_resize_data_url",
            side_effect=lambda value, *, target_bytes, max_edge: "data:image/jpeg;base64,wire-preview",
        ):
            modified = hooks._with_prefix_image_previews(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        updated = modified["input"][1]
        self.assertEqual(updated["id"], "fco_wire")
        self.assertEqual("input_text", updated["output"][0]["type"])
        advertised = self.reference_paths(updated["output"][0]["text"])
        self.assertEqual(advertised, ["/tmp/wire.png"])
        self.assertEqual(
            "data:image/jpeg;base64,wire-preview",
            updated["output"][1]["image_url"],
        )
        self.assertEqual(modified["input"][2], encrypted)
        # detail:"high" 不算显式原图请求
        self.assertFalse(
            hooks._codex_view_image_call_requests_original(wire_call)
        )

    def test_prefix_image_path_recent_mode_keeps_recent_previews_and_paths_older(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        cache_dir = self.use_replay_image_cache()
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        self.set_env(hooks._PREFIX_IMAGE_MODE_ENV, "path-recent")
        self.set_env(hooks._PREFIX_IMAGE_RECENT_COUNT_ENV, "1")

        def build() -> dict:
            items: list[dict] = []
            for i, path in enumerate(("/tmp/old1.png", "/tmp/old2.png", "/tmp/recent.png")):
                items.append({
                    "type": "function_call",
                    "call_id": f"call-{i}",
                    "name": "exec",
                    "arguments": '{"input":"await tools.view_image({path:\\"%s\\"});"}' % path,
                })
                items.append({
                    "type": "function_call_output",
                    "call_id": f"call-{i}",
                    "id": f"fco-{i}",
                    "output": [
                        {"type": "input_text", "text": "Script completed"},
                        {"type": "input_image", "image_url": "data:image/png;base64," + "X" * 150_000},
                    ],
                })
            items.append({"type": "reasoning", "encrypted_content": "opaque"})
            return {
                "call_type": "aresponses",
                "client_metadata": {
                    "x-codex-turn-metadata": '{"request_kind":"turn"}',
                },
                "input": items,
            }

        request = build()
        hooks._PREFIX_IMAGE_PREVIEW_CACHE.clear()
        with mock.patch.object(
            hooks._image_inputs_module,
            "_resize_data_url",
            side_effect=lambda value, *, target_bytes, max_edge: "data:image/jpeg;base64,preview",
        ):
            modified = hooks._with_prefix_image_previews(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        # 老的输出 -> 纯路径：图片 part 全部移除，引用文本带可重开的本地副本
        out0 = modified["input"][1]["output"]
        self.assertEqual([p.get("type") for p in out0], ["input_text", "input_text"])
        self.assertIn("replaced by local-path", out0[0]["text"])
        out1 = modified["input"][3]["output"]
        self.assertEqual([p.get("type") for p in out1], ["input_text", "input_text"])
        for output, client_path in ((out0, "/tmp/old1.png"), (out1, "/tmp/old2.png")):
            advertised = self.reference_paths(output[0]["text"])
            self.assertEqual(len(advertised), 1)
            # The reference is the client's own path: this proxy never invents
            # one, so a remote client's view_image can still open it.
            self.assertEqual(client_path, advertised[0])
        # 最近的输出 -> 预览 + 路径引用
        out2 = modified["input"][5]["output"]
        advertised_recent = self.reference_paths(out2[0]["text"])
        self.assertEqual(len(advertised_recent), 1)
        self.assertEqual("/tmp/recent.png", advertised_recent[0])
        self.assertEqual(list(cache_dir.iterdir()), [])
        image_parts2 = [p for p in out2 if p.get("type") == "input_image"]
        self.assertEqual(len(image_parts2), 1)
        self.assertEqual("data:image/jpeg;base64,preview", image_parts2[0]["image_url"])
        # 加密项逐字节不变
        self.assertEqual(modified["input"][6], request["input"][6])

        # 保留张数为 0 时所有超大输出都转成路径引用（不是全部保留）
        self.set_env(hooks._PREFIX_IMAGE_RECENT_COUNT_ENV, "0")
        hooks._PREFIX_IMAGE_PREVIEW_CACHE.clear()
        modified_zero = hooks._with_prefix_image_previews(build())
        assert modified_zero is not None
        for idx in (1, 3, 5):
            output = modified_zero["input"][idx]["output"]
            self.assertEqual([p.get("type") for p in output], ["input_text", "input_text"])
            self.assertIn("replaced by local-path", output[0]["text"])

        # off 模式完全不碰
        self.set_env(hooks._PREFIX_IMAGE_MODE_ENV, "off")
        with mock.patch.object(
            hooks._image_inputs_module, "_resize_data_url"
        ) as resize_mock:
            self.assertIsNone(hooks._with_prefix_image_previews(build()))
            resize_mock.assert_not_called()

        # preview 模式全部保留预览（近期张数无关）
        self.set_env(hooks._PREFIX_IMAGE_MODE_ENV, "preview")
        hooks._PREFIX_IMAGE_PREVIEW_CACHE.clear()
        with mock.patch.object(
            hooks._image_inputs_module,
            "_resize_data_url",
            side_effect=lambda value, *, target_bytes, max_edge: "data:image/jpeg;base64,preview",
        ):
            modified2 = hooks._with_prefix_image_previews(build())
        assert modified2 is not None
        for idx in (1, 3, 5):
            output = modified2["input"][idx]["output"]
            image_parts = [p for p in output if p.get("type") == "input_image"]
            self.assertEqual(len(image_parts), 1)
            self.assertEqual("data:image/jpeg;base64,preview", image_parts[0]["image_url"])

    def _single_aged_prefix_output_request(self, path: str, *, payload_char: str = "D") -> dict:
        """One oversized frozen ``view_image`` output that is never recent."""

        call = {
            "type": "custom_tool_call",
            "call_id": "call-aged",
            "name": "exec",
            "input": 'await tools.view_image({path:"%s"});' % path,
        }
        output = {
            "type": "custom_tool_call_output",
            "call_id": "call-aged",
            "id": "ctco_aged",
            "output": [
                {"type": "input_text", "text": "Script completed"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + (payload_char * 150_000),
                },
            ],
        }
        return {
            "call_type": "aresponses",
            "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'},
            "input": [
                call,
                output,
                {"type": "reasoning", "encrypted_content": "opaque"},
            ],
        }

    def test_prefix_image_path_reference_keeps_a_durable_client_path(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        cache_dir = self.use_replay_image_cache()
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        self.set_env(hooks._PREFIX_IMAGE_MODE_ENV, "path-recent")
        self.set_env(hooks._PREFIX_IMAGE_RECENT_COUNT_ENV, "0")

        # A workspace file the client owns keeps its own path: no copy needed.
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="young-router-durable-") as durable:
            durable_path = Path(durable) / "sheet.png"
            durable_path.write_bytes(b"durable sheet bytes")
            modified = hooks._with_prefix_image_previews(
                self._single_aged_prefix_output_request(str(durable_path))
            )

        self.assertIsNotNone(modified)
        assert modified is not None
        output = modified["input"][1]["output"]
        self.assertEqual([p.get("type") for p in output], ["input_text", "input_text"])
        self.assertEqual(self.reference_paths(output[0]["text"]), [str(durable_path)])
        self.assertEqual(list(cache_dir.iterdir()), [])

    def test_prefix_image_path_reference_survives_temporary_file_cleanup(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        cache_dir = self.use_replay_image_cache()
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        self.set_env(hooks._PREFIX_IMAGE_MODE_ENV, "path-recent")
        self.set_env(hooks._PREFIX_IMAGE_RECENT_COUNT_ENV, "0")

        # A clipboard paste or an intermediate crop lives in OS temp storage;
        # the reference must survive the client's later cleanup of it.
        with tempfile.TemporaryDirectory(prefix="young-router-crop-") as temporary:
            crop_path = Path(temporary) / "crop.png"
            crop_path.write_bytes(b"crop bytes")
            modified = hooks._with_prefix_image_previews(
                self._single_aged_prefix_output_request(str(crop_path))
            )
            self.assertIsNotNone(modified)
            assert modified is not None
            advertised = self.reference_paths(modified["input"][1]["output"][0]["text"])
            self.assertEqual(len(advertised), 1)
            # A remote gateway cannot see this path, and must not pretend it
            # can: it repeats the path the client's own call wrote and leaves
            # the bytes in the client's hands.
            self.assertEqual(advertised[0], str(crop_path))
            self.assertEqual(list(cache_dir.iterdir()), [])

    def test_prefix_image_path_reference_never_needs_a_local_copy(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        self.set_env(hooks._PREFIX_IMAGE_MODE_ENV, "path-recent")
        self.set_env(hooks._PREFIX_IMAGE_RECENT_COUNT_ENV, "0")

        hooks._PREFIX_IMAGE_PREVIEW_CACHE.clear()
        with mock.patch.object(
            hooks._image_inputs_module,
            "_resize_data_url",
            side_effect=lambda value, *, target_bytes, max_edge: "data:image/jpeg;base64,preview",
        ):
            modified = hooks._with_prefix_image_previews(
                self._single_aged_prefix_output_request("/tmp/lost-crop.png")
            )

        self.assertIsNotNone(modified)
        assert modified is not None
        output = modified["input"][1]["output"]
        # Nothing on this host is touched to honor the reference: the path is
        # the client's and stays valid on the client whatever this proxy can see.
        self.assertIn(
            hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER,
            json.dumps(output),
        )
        self.assertEqual(
            self.reference_paths(output[0]["text"]),
            ["/tmp/lost-crop.png"],
        )

    def test_prefix_image_previews_pair_split_identifier_wire_shapes(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        self.set_env(hooks._PREFIX_IMAGE_MODE_ENV, "path-recent")
        self.set_env(hooks._PREFIX_IMAGE_RECENT_COUNT_ENV, "1")

        def build() -> dict:
            items: list[dict] = []
            for i, path in enumerate(("/tmp/old1.png", "/tmp/old2.png", "/tmp/recent.png")):
                # Replayed wire splits the pairing identifiers: the call keeps
                # only its item ``id`` while the output keeps only ``call_id``,
                # and the two strings never agree with each other.
                items.append({
                    "type": "function_call",
                    "id": f"fc-{i}",
                    "name": "exec",
                    "arguments": 'const r = await tools.view_image({path:"%s", detail:"high"});' % path,
                })
                items.append({
                    "type": "function_call_output",
                    "call_id": f"wire-call-{i}",
                    "output": [
                        {"type": "input_text", "text": "Script completed"},
                        {"type": "input_image", "image_url": "data:image/png;base64," + "X" * 150_000},
                    ],
                })
            items.append({"type": "reasoning", "encrypted_content": "opaque"})
            return {
                "call_type": "aresponses",
                "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'},
                "input": items,
            }

        request = build()
        hooks._PREFIX_IMAGE_PREVIEW_CACHE.clear()
        with mock.patch.object(
            hooks._image_inputs_module,
            "_resize_data_url",
            side_effect=lambda value, *, target_bytes, max_edge: "data:image/jpeg;base64,preview",
        ):
            modified = hooks._with_prefix_image_previews(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        # 老的输出 -> 纯路径：图片 part 全部移除，引用文本带路径
        for idx in (1, 3):
            output = modified["input"][idx]["output"]
            self.assertEqual([p.get("type") for p in output], ["input_text", "input_text"])
            self.assertIn("replaced by local-path", output[0]["text"])
        # 最近的输出 -> 预览 + 路径引用
        output = modified["input"][5]["output"]
        image_parts = [p for p in output if p.get("type") == "input_image"]
        self.assertEqual(len(image_parts), 1)
        self.assertEqual("data:image/jpeg;base64,preview", image_parts[0]["image_url"])
        # 加密项逐字节不变
        self.assertEqual(modified["input"][6], request["input"][6])

    def test_prefix_image_original_path_converts_aged_original_outputs_by_default(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        cache_dir = self.use_replay_image_cache()
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        self.set_env(hooks._PREFIX_IMAGE_MODE_ENV, "path-recent")
        self.set_env(hooks._PREFIX_IMAGE_RECENT_COUNT_ENV, "1")
        # 默认开关开启：原图请求超出保留窗口后同样转成纯路径引用

        def build() -> dict:
            items: list[dict] = []
            for i, path in enumerate(("/tmp/orig-old1.png", "/tmp/orig-old2.png", "/tmp/orig-recent.png")):
                items.append({
                    "type": "function_call",
                    "call_id": f"call-orig-{i}",
                    "name": "exec",
                    "arguments": 'const r = await tools.view_image({path:"%s", detail:"original"});' % path,
                })
                items.append({
                    "type": "function_call_output",
                    "call_id": f"call-orig-{i}",
                    "output": [
                        {"type": "input_text", "text": "Script completed"},
                        {"type": "input_image", "image_url": "data:image/png;base64," + "O" * 150_000},
                    ],
                })
            items.append({"type": "reasoning", "encrypted_content": "opaque"})
            return {
                "call_type": "aresponses",
                "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'},
                "input": items,
            }

        request = build()
        hooks._PREFIX_IMAGE_PREVIEW_CACHE.clear()
        with mock.patch.object(
            hooks._image_inputs_module,
            "_resize_data_url",
            side_effect=lambda value, *, target_bytes, max_edge: "data:image/jpeg;base64,preview",
        ) as resize_mock:
            modified = hooks._with_prefix_image_previews(request)
            self.assertEqual(resize_mock.call_count, 1)

        self.assertIsNotNone(modified)
        assert modified is not None
        # 超出保留窗口的原图输出 -> 纯路径引用，图片 part 移除
        for idx in (1, 3):
            output = modified["input"][idx]["output"]
            self.assertEqual([p.get("type") for p in output], ["input_text", "input_text"])
            self.assertIn("replaced by local-path", output[0]["text"])
            advertised = self.reference_paths(output[0]["text"])
            self.assertEqual(len(advertised), 1)
            self.assertEqual(
                advertised[0],
                "/tmp/orig-old1.png" if idx == 1 else "/tmp/orig-old2.png",
            )
            self.assertEqual(list(cache_dir.iterdir()), [])
        # 保留窗口内的原图输出 -> 预览 + 路径引用（不再内联原始字节）
        recent_output = modified["input"][5]["output"]
        recent_images = [p for p in recent_output if p.get("type") == "input_image"]
        self.assertEqual(len(recent_images), 1)
        self.assertEqual(recent_images[0]["image_url"], "data:image/jpeg;base64,preview")
        self.assertIn(hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER, recent_output[0]["text"])
        advertised = self.reference_paths(recent_output[0]["text"])
        self.assertEqual(advertised, ["/tmp/orig-recent.png"])
        self.assertEqual(modified["input"][6], request["input"][6])

    def test_prefix_image_original_path_switch_off_preserves_original_outputs(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        self.set_env(hooks._PREFIX_IMAGE_MODE_ENV, "path-recent")
        self.set_env(hooks._PREFIX_IMAGE_RECENT_COUNT_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_ORIGINAL_PATH_ENV, "0")

        def build() -> dict:
            items: list[dict] = []
            for i, path in enumerate(("/tmp/orig-old1.png", "/tmp/orig-old2.png", "/tmp/orig-recent.png")):
                items.append({
                    "type": "function_call",
                    "call_id": f"call-orig-{i}",
                    "name": "exec",
                    "arguments": 'const r = await tools.view_image({path:"%s", detail:"original"});' % path,
                })
                items.append({
                    "type": "function_call_output",
                    "call_id": f"call-orig-{i}",
                    "output": [
                        {"type": "input_text", "text": "Script completed"},
                        {"type": "input_image", "image_url": "data:image/png;base64," + "O" * 150_000},
                    ],
                })
            items.append({"type": "reasoning", "encrypted_content": "opaque"})
            return {
                "call_type": "aresponses",
                "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'},
                "input": items,
            }

        # 开关关闭：原图输出全部跳过 -> 整个请求不变
        self.assertIsNone(hooks._with_prefix_image_previews(build()))

    def test_prefix_image_previews_skip_compaction_requests(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        call = {
            "type": "function_call",
            "call_id": "call-compact",
            "name": "exec",
            "arguments": '{"input":"await tools.view_image({path:\\"/tmp/comp.png\\"});"}',
        }
        output = {
            "type": "function_call_output",
            "call_id": "call-compact",
            "output": [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + ("C" * 150_000),
                }
            ],
        }
        request = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "input": [
                call,
                output,
                {"type": "reasoning", "encrypted_content": "opaque"},
                {"type": "compaction_trigger"},
            ],
        }
        with mock.patch.object(
            hooks._image_inputs_module, "_resize_data_url"
        ) as resize_mock:
            self.assertIsNone(hooks._with_prefix_image_previews(request))
            resize_mock.assert_not_called()

    def test_unsigned_history_drops_an_oversized_inline_image(self) -> None:
        """No signed prefix means nothing must stay byte-exact.

        The history still replays in full on every turn, so an oversized
        inline image in it is dead weight: the gateway counts those bytes and
        the client discounts them, which is how a request grows past the
        gateway's ceiling while the client still shows headroom.  An image
        whose call names no path cannot be replayed by reference, so its bytes
        become a note.
        """

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        call = {
            "type": "custom_tool_call",
            "call_id": "call-fresh",
            # A ``js`` screenshot names no path, so nothing can reopen it.
            "name": "js",
            "input": "await page.screenshot();",
        }
        output = {
            "type": "custom_tool_call_output",
            "call_id": "call-fresh",
            "output": [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + ("C" * 150_000),
                }
            ],
        }
        request = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [call, output],
        }
        with mock.patch.object(
            hooks._image_inputs_module, "_resize_data_url"
        ) as resize_mock:
            updated = hooks._with_prefix_image_previews(request)
            resize_mock.assert_not_called()
        self.assertIsNotNone(updated)
        assert updated is not None
        parts = updated["input"][1]["output"]
        self.assertEqual(parts[0]["type"], "input_text")
        self.assertIn("elided", parts[0]["text"])

    def test_unsigned_history_keeps_an_image_a_path_can_reopen(self) -> None:
        """An image with a path behind it belongs to the path-reference pass.

        Eliding it here would throw away an original the model can still open.
        """

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        path = "/tmp/reopenable.png"
        request = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call-path",
                    "name": "exec",
                    "input": f'await tools.view_image({{path:"{path}"}});',
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-path",
                    "output": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64," + ("C" * 150_000),
                        }
                    ],
                },
            ],
        }
        elided = hooks._with_elided_oversized_history_images(
            request, request["input"]
        )
        self.assertIsNone(elided)

    def test_native_view_image_call_exposes_its_path(self) -> None:
        """The current client ships ``view_image`` as its own function call.

        Only the older ``exec`` script shape was parsed, so a native call's
        ``{"path": ...}`` arguments were invisible to every replay pass: the
        image looked un-reopenable and an unsigned history replaced it with a
        note telling the model to run the tool again -- which the model did, on
        every later turn, because the fresh capture was elided the same way.
        """

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        call = {
            "type": "function_call",
            "id": "fc_native",
            "name": "view_image",
            "namespace": "functions",
            "arguments": '{"path":"/tmp/yr_shot.png"}',
            "call_id": "call-native",
        }
        self.assertEqual(
            hooks._codex_view_image_paths_from_call(call),
            ["/tmp/yr_shot.png"],
        )
        self.assertFalse(hooks._codex_view_image_call_requests_original(call))
        original_call = dict(call, arguments='{"path":"/tmp/yr_shot.png","detail":"original"}')
        self.assertTrue(hooks._codex_view_image_call_requests_original(original_call))

    def test_unsigned_native_view_image_is_previewed_not_elided(self) -> None:
        """A path-backed native image keeps a preview and a re-openable path.

        The note the elision writes invites a re-capture loop; a path-backed
        image never needs it, because the same preview budget plus the matching
        path keeps both the model's view and the replay small.
        """

        import base64
        import io
        import os

        from PIL import Image

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.use_replay_image_cache()
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        image = Image.frombytes("RGB", (1400, 1100), os.urandom(1400 * 1100 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        oversized_url = (
            "data:image/jpeg;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        )
        request = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "function_call",
                    "id": "fc_native",
                    "name": "view_image",
                    "namespace": "functions",
                    "arguments": '{"path":"/tmp/yr_shot.png"}',
                    "call_id": "call-native",
                },
                {
                    "type": "function_call_output",
                    "id": "fco_native",
                    "call_id": "call-native",
                    "output": [
                        {"type": "input_image", "image_url": oversized_url},
                    ],
                },
            ],
        }

        modified = hooks._with_prefix_image_previews(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        output = modified["input"][1]["output"]
        self.assertIn(hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER, output[0]["text"])
        self.assertNotIn("elided", output[0]["text"])
        self.assertLess(
            hooks._image_data_url_size(output[1]["image_url"]),
            hooks._image_data_url_size(oversized_url),
        )
        # The path comes from the call itself and is repeated verbatim.
        self.assertEqual(
            self.reference_paths(output[0]["text"]),
            ["/tmp/yr_shot.png"],
        )

    def test_codex_view_image_reopen_marks_repeated_path_as_original(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        path = "/tmp/original.png"
        previous_output = {
            "type": "custom_tool_call_output",
            "call_id": "call-first",
            "output": [
                {
                    "type": "input_text",
                    "text": (
                        f"{hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER}\n"
                        f"Inline images below are reduced previews.\n1. {path}"
                    ),
                }
            ],
        }
        current_call = {
            "type": "custom_tool_call",
            "call_id": "call-reopen",
            "name": "exec",
            "input": f'await tools.view_image({{path:"{path}", detail:"high"}});',
        }
        current_output = {
            "type": "custom_tool_call_output",
            "call_id": "call-reopen",
            "output": [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,abc",
                }
            ],
        }
        original = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [previous_output, current_call, current_output],
        }

        modified = hooks._with_codex_view_image_output_paths(original)

        self.assertIsNotNone(modified)
        assert modified is not None
        text = modified["input"][2]["output"][0]["text"]
        self.assertIn(hooks._CODEX_VIEW_IMAGE_ORIGINAL_REFERENCE_MARKER, text)

    def test_codex_view_image_original_detail_is_explicit(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module

        self.assertTrue(
            hooks._codex_view_image_call_requests_original(
                {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "input": 'await tools.view_image({path:"/tmp/a.png", detail:"original"});',
                }
            )
        )
        self.assertFalse(
            hooks._codex_view_image_call_requests_original(
                {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "input": 'await tools.view_image({path:"/tmp/a.png", detail:"high"});',
                }
            )
        )

        request = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call-original",
                    "name": "exec",
                    "input": (
                        'await tools.view_image({path:"/tmp/a.png", '
                        'detail:"original"});'
                    ),
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-original",
                    "output": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,abc",
                        }
                    ],
                },
            ],
        }
        modified = hooks._with_codex_view_image_output_paths(request)
        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertIn(
            hooks._CODEX_VIEW_IMAGE_ORIGINAL_REFERENCE_MARKER,
            modified["input"][1]["output"][0]["text"],
        )

    def test_codex_view_image_preview_target_scales_between_128_and_320_kb(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module

        self.assertEqual(
            hooks._codex_view_image_preview_target(1),
            hooks._CODEX_VIEW_IMAGE_PREVIEW_MAX_TARGET_BYTES,
        )
        self.assertEqual(hooks._codex_view_image_preview_target(8), 300_000)
        self.assertEqual(hooks._codex_view_image_preview_target(12), 200_000)
        self.assertEqual(
            hooks._codex_view_image_preview_target(19),
            hooks._CODEX_VIEW_IMAGE_PREVIEW_MIN_TARGET_BYTES,
        )
        self.assertGreaterEqual(
            hooks._codex_view_image_preview_target(100),
            hooks._CODEX_VIEW_IMAGE_PREVIEW_MIN_TARGET_BYTES,
        )

    async def test_pre_call_view_image_output_uses_path_backed_preview_budget(self) -> None:
        import base64
        import io
        import os

        from PIL import Image

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        image = Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        original_url = (
            "data:image/jpeg;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        )
        original = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call-image",
                    "name": "exec",
                    "input": (
                        "const preview = await tools.view_image({path:"
                        "\"/Users/example/project/qa/preview.png\", detail:\"high\"});"
                        " image(preview.image_url);"
                    ),
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-image",
                    "output": [
                        {"type": "input_image", "image_url": original_url},
                    ],
                },
            ],
        }

        modified = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        output = modified["input"][1]["output"]
        self.assertIn(hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER, output[0]["text"])
        self.assertLessEqual(
            hooks._image_data_url_size(output[1]["image_url"]),
            hooks._CODEX_VIEW_IMAGE_PREVIEW_TARGET_BYTES,
        )
        self.assertGreater(
            hooks._image_data_url_size(output[1]["image_url"]),
            16_384,
        )
        self.assertEqual(original["input"][1]["output"][0]["image_url"], original_url)

    async def test_pre_call_explicit_reopen_is_bounded_to_the_largest_forwarded_view(self) -> None:
        import base64
        import io
        import os

        from PIL import Image

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.use_replay_image_cache()
        path = "/Users/example/project/qa/original.png"
        image = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        original_url = (
            "data:image/jpeg;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        )
        previous_output = {
            "type": "custom_tool_call_output",
            "call_id": "call-first",
            "output": [
                {
                    "type": "input_text",
                    "text": (
                        f"{hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER}\n"
                        f"Inline images below are reduced previews.\n1. {path}"
                    ),
                }
            ],
        }
        original = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                previous_output,
                {
                    "type": "custom_tool_call",
                    "call_id": "call-reopen",
                    "name": "exec",
                    "input": (
                        "const original = await tools.view_image({path:"
                        f'"{path}", detail:"original"}}); '
                        "image(original.image_url);"
                    ),
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-reopen",
                    "output": [
                        {"type": "input_image", "image_url": original_url},
                    ],
                },
            ],
        }

        modified = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        output = modified["input"][2]["output"]
        self.assertIn(
            hooks._CODEX_VIEW_IMAGE_ORIGINAL_REFERENCE_MARKER,
            output[0]["text"],
        )
        # An explicit original-resolution reopen is not an exemption from the
        # request budget: it keeps the single-image representation (larger and
        # higher resolution than the shared preview) and its local path.
        self.assertNotEqual(output[1]["image_url"], original_url)
        self.assertLessEqual(
            hooks._image_data_url_size(output[1]["image_url"]),
            hooks._INLINE_IMAGE_SINGLE_TARGET_BYTES,
        )
        self.assertGreater(
            hooks._image_data_url_size(output[1]["image_url"]),
            16_384,
        )
        # The reference repeats the path the client's own call wrote, whatever
        # this host can see of it: that is the one a re-open can reach.
        advertised = self.reference_paths(output[0]["text"])
        self.assertEqual(advertised, [path])
        self.assertEqual(original["input"][2]["output"][0]["image_url"], original_url)

    async def test_iterative_original_image_is_bounded_after_encrypted_boundary(self) -> None:
        import base64
        import io
        import os

        from PIL import Image

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.use_replay_image_cache()
        path = "/Users/example/project/qa/iterative-original.png"
        image = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        original_url = (
            "data:image/jpeg;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        )
        # The client re-sends its own history unchanged on every turn.
        client_items = [
            {
                "type": "custom_tool_call",
                "call_id": "call-first",
                "name": "exec",
                "input": (
                    "const preview = await tools.view_image({path:"
                    f'"{path}", detail:"high"}}); '
                    "image(preview.image_url);"
                ),
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call-first",
                "output": [
                    {"type": "input_image", "image_url": original_url},
                ],
            },
            {
                "type": "custom_tool_call",
                "call_id": "call-original",
                "name": "exec",
                "input": (
                    "const original = await tools.view_image({path:"
                    f'"{path}", detail:"original"}}); '
                    "image(original.image_url);"
                ),
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call-original",
                "output": [
                    {"type": "input_image", "image_url": original_url},
                ],
            },
        ]
        first_request = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [dict(item) for item in client_items],
        }
        self.addCleanup(
            self.set_env, hooks._HISTORY_TOOL_OUTPUT_KEPT_CHARS_ENV, None
        )
        # Two multi-megabyte originals are a replayed tool-call tail larger
        # than the window, so the window pass would empty the older one before
        # the image pass ever saw it.  Raise the window for this test, which is
        # about the image pass's own treatment of both halves.
        self.set_env(hooks._HISTORY_TOOL_OUTPUT_KEPT_CHARS_ENV, "5000000")
        first = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            first_request,
            call_type="aresponses",
        )
        self.assertIsNotNone(first)
        assert first is not None
        first_preview = first["input"][1]["output"][1]["image_url"]
        self.assertLessEqual(
            hooks._image_data_url_size(first_preview),
            hooks._CODEX_VIEW_IMAGE_PREVIEW_MAX_TARGET_BYTES,
        )
        # The explicit reopen keeps the larger single-image representation while
        # it is the current turn's tool result.
        first_original = first["input"][3]["output"]
        self.assertIn(
            hooks._CODEX_VIEW_IMAGE_ORIGINAL_REFERENCE_MARKER,
            first_original[0]["text"],
        )
        self.assertLessEqual(
            hooks._image_data_url_size(first_original[1]["image_url"]),
            hooks._INLINE_IMAGE_SINGLE_TARGET_BYTES,
        )

        # Once a later item carries encrypted_content both outputs replay from
        # the frozen prefix: each one is a bounded preview plus its local path
        # instead of multi-megabyte original bytes.
        second_request = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                *[dict(item) for item in client_items],
                {"type": "reasoning", "encrypted_content": "opaque-history"},
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Continue."}],
                },
            ],
        }
        second = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            second_request,
            call_type="aresponses",
        )
        self.assertIsNotNone(second)
        assert second is not None
        for call_id in ("call-first", "call-original"):
            frozen = next(
                item
                for item in second["input"]
                if item.get("call_id") == call_id
                and item.get("type") == "custom_tool_call_output"
            )
            self.assertIn(
                hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER,
                frozen["output"][0]["text"],
            )
            images = [
                part
                for part in frozen["output"]
                if part.get("type") == "input_image"
            ]
            self.assertEqual(len(images), 1)
            self.assertLessEqual(
                hooks._image_data_url_size(images[0]["image_url"]),
                hooks._CODEX_VIEW_IMAGE_PREVIEW_MAX_TARGET_BYTES,
            )
        # Both replayed outputs advertise a durable path for the original file.
        self.assertIn(path, json.dumps(second["input"]))
        # The client's own history is never mutated.
        self.assertEqual(
            client_items[3]["output"][0]["image_url"],
            original_url,
        )

    async def test_pre_call_does_not_inject_truncation_for_oversized_signed_image_history(self) -> None:
        import base64

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        old_image = "data:image/png;base64," + base64.b64encode(
            b"x" * (hooks._INLINE_IMAGE_MANY_TOTAL_TARGET_BYTES + 1)
        ).decode("ascii")
        original = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call-old-preview",
                    "name": "exec",
                    "input": 'await tools.view_image({path:"/tmp/old-preview.png"});',
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-old-preview",
                    "output": [
                        {"type": "input_image", "image_url": old_image},
                    ],
                },
                {
                    "type": "reasoning",
                    "encrypted_content": "opaque-signed-history",
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Continue."}],
                },
            ],
        }

        modified = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertNotIn("truncation", modified)
        self.assertIs(modified["input"], original["input"])
        self.assertEqual(
            modified["input"][1]["output"][0]["image_url"],
            old_image,
        )

    async def test_pre_call_preserves_unavailable_request_user_input_history(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "model_info": {
                "supports_responses_web_search": False,
            },
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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

    async def test_pre_call_deployment_hook_replaces_our_own_proxy_user_agent(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
        original = {
            "api_base": "https://example.com/v1",
            "extra_headers": {"X-Trace": "keep-me"},
            "litellm_params": {
                "proxy_server_request": {
                    "headers": {
                        "User-Agent": "Young%20Router/1 CFNetwork/3860.600.21 Darwin/25.5.0",
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
            hooks._FALLBACK_BROWSER_USER_AGENT,
        )
        self.assertNotIn("Accept", modified["extra_headers"])
        self.assertEqual(original["extra_headers"], {"X-Trace": "keep-me"})

    async def test_pre_call_deployment_hook_preserves_explicit_litellm_headers(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
        original = {
            "api_base": "https://example.com/v1",
            "extra_headers": {
                "X-LiteLLM-Model-Name": "downstream-explicit",
                "User-Agent": "LiteLLM downstream-client/1",
            },
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNone(modified)
        self.assertEqual(
            original["extra_headers"],
            {
                "X-LiteLLM-Model-Name": "downstream-explicit",
                "User-Agent": "LiteLLM downstream-client/1",
            },
        )

    async def test_pre_call_deployment_hook_codex_user_agent_overrides_old_extra_header(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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

    async def test_pre_call_preserves_realistic_structured_compaction_request_shape(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
        image_url = "data:image/png;base64," + "A" * 180_000
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "client_metadata": {
                "session_id": "thread-structured-compaction",
                "thread_id": "thread-structured-compaction",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "functions",
                            "tools": [{"type": "function", "name": "exec"}],
                        }
                    ],
                },
                {
                    "type": "message",
                    "id": "msg_image",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Inspect this image."},
                        {"type": "input_image", "image_url": image_url, "detail": "high"},
                    ],
                },
                {
                    "type": "custom_tool_call",
                    "id": "call_item",
                    "call_id": "call_structured",
                    "name": "exec",
                    "input": '{"cmd":"inspect"}',
                    "status": "completed",
                },
                {
                    "type": "custom_tool_call_output",
                    "id": "output_item",
                    "call_id": "call_structured",
                    "output": [
                        {"type": "input_text", "text": "x" * 1_100_000},
                        {"type": "input_image", "image_url": image_url, "detail": "high"},
                    ],
                },
                {
                    "type": "reasoning",
                    "id": "reasoning_signed",
                    "summary": [],
                    "encrypted_content": "opaque-encrypted-reasoning",
                },
                {
                    "type": "compaction",
                    "id": "compaction_previous",
                    "encrypted_content": "opaque-encrypted-compaction",
                },
                {"type": "compaction_trigger"},
            ],
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }
        original_input_json = json.dumps(
            original["input"],
            ensure_ascii=False,
            separators=(",", ":"),
        )

        modified = await hook.async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            json.dumps(
                modified["input"],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            original_input_json,
        )
        self.assertEqual(modified["tools"], [])
        self.assertEqual(modified["tool_choice"], "auto")
        self.assertFalse(modified["parallel_tool_calls"])
        self.assertEqual(
            modified["input"][4]["encrypted_content"],
            "opaque-encrypted-reasoning",
        )
        self.assertEqual(
            modified["input"][5]["encrypted_content"],
            "opaque-encrypted-compaction",
        )
        self.assertEqual(modified["input"][1]["content"][1]["image_url"], image_url)
        self.assertEqual(modified["input"][3]["output"][1]["image_url"], image_url)

    async def test_pre_call_deployment_hook_keeps_ordinary_codex_turn_byte_for_byte(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
        original = {"api_base": "https://example.com/v1"}

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        # An unrelated host keeps its request untouched apart from the one
        # User-Agent guarantee: nothing of ours identifies the router upstream.
        self.assertEqual(
            {"api_base": "https://example.com/v1", "extra_headers": {"User-Agent": hooks._FALLBACK_BROWSER_USER_AGENT}},
            modified,
        )

    async def test_pre_call_deployment_hook_uses_browser_header_retry_marker(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()
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
        contract = hooks._codex_contract_module
        hook = hooks.YoungRouterHook()

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
        contract = hooks._codex_contract_module

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
        contract = hooks._codex_contract_module

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
        contract = hooks._codex_contract_module

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
        contract = hooks._codex_contract_module
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

    def test_prefix_image_previews_bound_recent_original_resolution_outputs(self) -> None:
        """A recent ``detail: original`` output no longer replays its raw bytes.

        Re-opening several crops at original resolution kept tens of megabytes
        of history inline, which made every turn too large for the upstream
        gateways; the preview plus its durable path keeps the replay small
        while the model can still re-open the original file.
        """

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        cache_dir = self.use_replay_image_cache()
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        self.set_env(hooks._PREFIX_IMAGE_MODE_ENV, "path-recent")
        self.set_env(hooks._PREFIX_IMAGE_RECENT_COUNT_ENV, "6")
        durable_path = Path(cache_dir) / "derived_crop.png"
        durable_path.write_bytes(b"the original crop bytes")
        oversized_url = "data:image/png;base64," + ("A" * 200_000)
        call = {
            "type": "custom_tool_call",
            "call_id": "call-original",
            "name": "exec",
            "input": (
                "const r = await tools.view_image({path:\"" + str(durable_path)
                + "\", detail:\"original\"}); image(r.image_url,\"original\");"
            ),
        }
        output = {
            "type": "custom_tool_call_output",
            "call_id": "call-original",
            "output": [
                {"type": "input_text", "text": "Script completed"},
                {"type": "input_image", "image_url": oversized_url, "detail": "original"},
            ],
        }
        encrypted = {
            "type": "reasoning",
            "encrypted_content": "opaque-signed-history",
        }
        original = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [call, output, encrypted],
        }

        resize_calls: list[tuple[int, int]] = []

        def fake_resize(value: str, *, target_bytes: int, max_edge: int) -> str:
            resize_calls.append((target_bytes, max_edge))
            return "data:image/jpeg;base64,cHJldmlldw=="

        with mock.patch.object(
            hooks._image_inputs_module, "_resize_data_url", side_effect=fake_resize
        ):
            hooks._PREFIX_IMAGE_PREVIEW_CACHE.clear()
            modified = hooks._with_prefix_image_previews(original)

        self.assertIsNotNone(modified)
        assert modified is not None
        updated = modified["input"][1]
        reference = updated["output"][0]
        # The explicit original-resolution request is replayed at the shared
        # preview fidelity instead of its original bytes.
        self.assertEqual(
            resize_calls,
            [
                (
                    hooks._CODEX_VIEW_IMAGE_PREVIEW_MIN_TARGET_BYTES,
                    hooks._INLINE_IMAGE_MANY_MAX_EDGE,
                )
            ],
        )
        self.assertEqual(
            updated["output"][2]["image_url"],
            "data:image/jpeg;base64,cHJldmlldw==",
        )
        self.assertEqual(updated["output"][2]["detail"], "original")
        self.assertIn(hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER, reference["text"])
        # The reference repeats the path this test's own call named.
        self.assertEqual(
            self.reference_paths(reference["text"]),
            [str(durable_path)],
        )
        # The call item and the signed item stay byte-identical, and the
        # client's own history is never mutated.
        self.assertEqual(modified["input"][0], call)
        self.assertEqual(modified["input"][2], encrypted)
        self.assertEqual(original["input"][1]["output"][1]["image_url"], oversized_url)

    def test_bounded_image_inputs_bound_original_resolution_outputs(self) -> None:
        """A ``detail: original`` output keeps the highest forwarded resolution.

        The marker text still promises the model its original view, so the
        bytes are bounded to the single-image representation instead of being
        exempt from the request budget.
        """

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        oversized_url = "data:image/png;base64," + ("D" * 1_400_000)
        reference_text = (
            f"{hooks._CODEX_VIEW_IMAGE_ORIGINAL_REFERENCE_MARKER}\n"
            "Original-resolution image requested for this explicit re-open.\n"
            f"{hooks._CODEX_VIEW_IMAGE_REFERENCE_MARKER}\n"
            "1. /tmp/original.png"
        )
        output_item = {
            "type": "custom_tool_call_output",
            "call_id": "call-original",
            "output": [
                {"type": "input_text", "text": reference_text},
                {"type": "input_image", "image_url": oversized_url, "detail": "original"},
            ],
        }
        encrypted = {
            "type": "reasoning",
            "encrypted_content": "opaque-signed-history",
        }
        request = {
            "call_type": "aresponses",
            "input": [encrypted, output_item],
        }

        resize_calls: list[tuple[int, int]] = []

        def fake_resize(value: str, *, target_bytes: int, max_edge: int) -> str:
            resize_calls.append((target_bytes, max_edge))
            return "data:image/jpeg;base64,Ym91bmRlZA=="

        with mock.patch.object(
            hooks._image_inputs_module, "_resize_data_url", side_effect=fake_resize
        ):
            modified = hooks._with_bounded_image_inputs(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            resize_calls,
            [
                (
                    hooks._INLINE_IMAGE_SINGLE_TARGET_BYTES,
                    hooks._INLINE_IMAGE_SINGLE_MAX_EDGE,
                )
            ],
        )
        updated = modified["input"][1]
        self.assertEqual(updated["output"][0], output_item["output"][0])
        self.assertEqual(
            updated["output"][1]["image_url"],
            "data:image/jpeg;base64,Ym91bmRlZA==",
        )
        self.assertEqual(updated["output"][1]["detail"], "original")
        # The client's own history keeps its original bytes.
        self.assertEqual(request["input"][1]["output"][1]["image_url"], oversized_url)
        self.assertEqual(modified["input"][0], encrypted)

    def test_bounded_image_inputs_leave_small_original_outputs_untouched(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        small_url = "data:image/png;base64," + ("E" * 400)
        output_item = {
            "type": "custom_tool_call_output",
            "call_id": "call-original",
            "output": [
                {
                    "type": "input_text",
                    "text": hooks._CODEX_VIEW_IMAGE_ORIGINAL_REFERENCE_MARKER,
                },
                {"type": "input_image", "image_url": small_url, "detail": "original"},
            ],
        }
        request = {"call_type": "aresponses", "input": [output_item]}

        self.assertIsNone(hooks._with_bounded_image_inputs(request))

    def test_neutral_attachment_paths_move_router_literals_out_of_replay(self) -> None:
        """A replayed tool call never keeps naming the router's own storage.

        The model reads its own past commands back, so a router-owned path in
        its history keeps it reading and writing inside the router even after
        every advertised path became neutral.
        """

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        cache_dir = self.use_replay_image_cache()
        legacy_root = tempfile.TemporaryDirectory(prefix="young-router-legacy-")
        self.addCleanup(legacy_root.cleanup)
        self.set_env("YOUNG_ROUTER_HOME", legacy_root.name)
        legacy_cache = Path(legacy_root.name) / "image-cache"
        legacy_cache.mkdir(parents=True, exist_ok=True)
        source = legacy_cache / "6984af2576d7c2ebf38f2033f4ba258e.jpg"
        source.write_bytes(b"original photo bytes")
        derived = legacy_cache / "derived_B_top.png"
        derived.write_bytes(b"model crop")
        hidden = legacy_cache / "notes.txt"
        hidden.write_bytes(b"not an image")
        crop_output = legacy_cache / "scale.png"
        original = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call-view",
                    "name": "exec",
                    "input": (
                        "const r = await tools.view_image({"
                        f'path:"{source}", detail:"original"}}); '
                        "image(r.image_url)"
                    ),
                },
                {
                    "type": "custom_tool_call",
                    "call_id": "call-crop",
                    "name": "exec",
                    "input": (
                        f"sips -c 2100 2200 '{derived}' --out '{crop_output}'"
                    ),
                },
                {
                    "type": "custom_tool_call",
                    "call_id": "call-read",
                    "name": "exec",
                    "input": f"cat {hidden}",
                },
                {
                    "type": "custom_tool_call",
                    "call_id": "call-other",
                    "name": "exec",
                    "input": "sips -c 10 10 '/tmp/elsewhere/photo.jpg' --out '/tmp/elsewhere/out.png'",
                },
            ],
        }

        modified = hooks._with_neutral_attachment_paths(original)

        self.assertIsNotNone(modified)
        assert modified is not None
        view_script = modified["input"][0]["input"]
        self.assertNotIn(str(legacy_root.name), view_script)
        self.assertIn(str(cache_dir), view_script)
        view_copy = Path(view_script.split('path:"', 1)[1].split('"', 1)[0])
        self.assertEqual(view_copy.parent, cache_dir)
        self.assertEqual(view_copy.read_bytes(), b"original photo bytes")
        crop_script = modified["input"][1]["input"]
        self.assertNotIn(str(legacy_root.name), crop_script)
        crop_copy = Path(crop_script.split("'", 1)[1].split("'", 1)[0])
        self.assertEqual(crop_copy.parent, cache_dir)
        self.assertEqual(crop_copy.read_bytes(), b"model crop")
        # The crop command's own output path follows the same substitution, so
        # new files land beside the same source directory.
        crop_output = Path(crop_script.split("--out '", 1)[1].split("'", 1)[0])
        self.assertEqual(crop_output.parent, cache_dir)
        self.assertIn("scale", crop_output.name)
        # A non-image router file is never copied into a model-visible place,
        # a client-owned path is left alone, and the client history is intact.
        self.assertIn(str(hidden), modified["input"][2]["input"])
        self.assertEqual(modified["input"][3], original["input"][3])
        self.assertEqual(original["input"][0]["input"], original["input"][0]["input"])
        self.assertIn(str(legacy_root.name), original["input"][0]["input"])
        self.assertFalse((cache_dir / "notes.txt").exists())

    def test_replay_restores_a_client_image_the_client_deleted(self) -> None:
        """A vanished client image file is restored at the path the replay names.

        A clipboard paste or an intermediate crop the client has since deleted
        still appears in the replayed history, and a re-open against it fails
        with "no such file" -- which sends the model back to crop and view the
        same region again. The bytes are still in the request, so the file is
        restored where the call reads it.
        """

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        temporary = tempfile.TemporaryDirectory(prefix="image-client-path-")
        self.addCleanup(temporary.cleanup)
        vanished = Path(temporary.name) / "codex-clipboard-1234.jpg"
        payload = b"pasted photo bytes"
        request = {
            "call_type": "aresponses",
            "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'},
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call-paste",
                    "name": "exec",
                    "input": f'await tools.view_image({{path:"{vanished}", detail:"original"}});',
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-paste",
                    "output": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/jpeg;base64,"
                            + base64.b64encode(payload).decode("ascii"),
                        }
                    ],
                },
            ],
        }
        self.assertFalse(vanished.exists())

        modified = hooks._with_neutral_attachment_paths(request)

        self.assertIsNotNone(modified)
        self.assertTrue(vanished.is_file())
        self.assertEqual(vanished.read_bytes(), payload)
        # An existing file is never rewritten, and a path without paired image
        # bytes (a command's own future output) stays untouched.
        assert modified is not None
        second = {
            "call_type": "aresponses",
            "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'},
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call-crop",
                    "name": "exec",
                    "input": f"sips -c 10 10 '{vanished}' --out '{temporary.name}/new.png'",
                }
            ],
        }
        hooks._with_neutral_attachment_paths(second)
        self.assertEqual(vanished.read_bytes(), payload)

    def test_neutral_attachment_paths_rewrite_unquoted_and_note_paths(self) -> None:
        """Command substitutions, output echoes, and notes follow the same rule."""

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        cache_dir = self.use_replay_image_cache()
        legacy_root = tempfile.TemporaryDirectory(prefix="young-router-legacy-")
        self.addCleanup(legacy_root.cleanup)
        self.set_env("YOUNG_ROUTER_HOME", legacy_root.name)
        legacy_cache = Path(legacy_root.name) / "image-cache"
        legacy_cache.mkdir(parents=True, exist_ok=True)
        photo = legacy_cache / "crop.png"
        photo.write_bytes(b"crop bytes")
        config = Path(legacy_root.name) / "config.yaml"
        config.write_bytes(b"secret: keep-me")
        request = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call-cmd",
                    "name": "exec",
                    "input": f"sips -g pixelWidth {photo} && echo done",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-cmd",
                    "output": [
                        {"type": "input_text", "text": f"Warning: wrote {photo}! `{photo}`"},
                    ],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"Only reopen these durable paths: {photo}",
                        }
                    ],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"Never read {config} or {legacy_cache / 'notes.txt'}",
                        }
                    ],
                },
            ],
        }

        modified = hooks._with_neutral_attachment_paths(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        original_text = json.dumps(original_input := request["input"])
        self.assertIn(str(legacy_root.name), original_text)
        forwarded_text = json.dumps(modified["input"])
        self.assertNotIn(str(photo), forwarded_text)
        copies = sorted(cache_dir.glob("*.png"))
        self.assertEqual(len(copies), 1)
        self.assertIn(str(copies[0]), forwarded_text)
        self.assertEqual(copies[0].read_bytes(), b"crop bytes")
        # Non-image router files are never copied into a model-visible place.
        self.assertIn(str(config), modified["input"][3]["content"][0]["text"])
        self.assertIn(str(legacy_cache / "notes.txt"), modified["input"][3]["content"][0]["text"])
        self.assertFalse((cache_dir / "notes.txt").exists())
        self.assertFalse((cache_dir / "config.yaml").exists())

    def test_fresh_crops_keep_their_zoom_while_history_keeps_previews(self) -> None:
        """The model's own crop keeps up to 2200 px; history replays as previews.

        Cropping a region at several thousand pixels is how the model reads
        handwriting. Replaying that crop at the shared 1400 px preview edge threw
        the zoom away, so the model cropped the same region again and again.
        """

        import base64
        import io
        import os

        from PIL import Image

        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        image = Image.frombytes("RGB", (4000, 2500), os.urandom(4000 * 2500 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        fresh_url = (
            "data:image/jpeg;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        )
        self.assertGreater(
            hooks._image_data_url_size(fresh_url),
            hooks._INLINE_IMAGE_SINGLE_TARGET_BYTES,
        )
        crop_call = {
            "type": "custom_tool_call",
            "call_id": "call-crop",
            "name": "exec",
            "input": "sips -c 2400 3900 --cropOffset 0 0 photo.jpg --out crop.png",
        }
        crop_output = {
            "type": "custom_tool_call_output",
            "call_id": "call-crop",
            "output": [
                {"type": "input_text", "text": "Script completed"},
                {"type": "input_image", "image_url": fresh_url},
            ],
        }
        encrypted = {"type": "reasoning", "encrypted_content": "opaque"}
        request = {
            "call_type": "aresponses",
            "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'},
            "input": [encrypted, crop_call, crop_output],
        }

        modified = hooks._with_bounded_image_inputs(request)

        self.assertIsNotNone(modified)
        assert modified is not None
        image_url = modified["input"][2]["output"][1]["image_url"]
        self.assertLessEqual(
            hooks._image_data_url_size(image_url),
            hooks._INLINE_IMAGE_SINGLE_TARGET_BYTES,
        )
        with Image.open(
            io.BytesIO(base64.b64decode(image_url.split(",", 1)[1]))
        ) as resized:
            # Above the shared preview edge even for this worst-case noisy image
            # (a real crop lands at the full 2200 px), so the zoom survives.
            self.assertGreater(resized.size[0], hooks._INLINE_IMAGE_MANY_MAX_EDGE)
        # The client's own bytes are untouched.
        self.assertEqual(request["input"][2]["output"][1]["image_url"], fresh_url)

    def test_history_previews_still_use_the_shared_preview_edge(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "1")
        self.set_env(hooks._PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "1000")
        seen: list[tuple[int, int]] = []

        def fake_resize(value: str, *, target_bytes: int, max_edge: int) -> str:
            seen.append((target_bytes, max_edge))
            return "data:image/jpeg;base64,cHJldmlldw=="

        request = self._prefix_preview_request()
        with mock.patch.object(
            hooks._image_inputs_module, "_resize_data_url", side_effect=fake_resize
        ):
            hooks._PREFIX_IMAGE_PREVIEW_CACHE.clear()
            modified = hooks._with_prefix_image_previews(request)

        self.assertIsNotNone(modified)
        self.assertEqual(
            seen,
            [
                (
                    hooks._CODEX_VIEW_IMAGE_PREVIEW_MIN_TARGET_BYTES,
                    hooks._INLINE_IMAGE_MANY_MAX_EDGE,
                )
            ],
        )


class HookResponsesToolOutputReplayWindowTests(HookTestCase):
    """The replayed tool-call tail is bounded in characters, not in outputs."""

    def _request(self, pairs: int, payload_chars: int) -> dict:
        items: list[dict] = []
        for index in range(pairs):
            items.append(
                {
                    "type": "function_call",
                    "id": f"fc_{index}",
                    "name": "exec_command",
                    "namespace": "functions",
                    "arguments": '{"cmd":"ls"}',
                    "call_id": f"call_{index}",
                }
            )
            items.append(
                {
                    "type": "function_call_output",
                    "id": f"fco_{index}",
                    "call_id": f"call_{index}",
                    "output": "x" * payload_chars,
                }
            )
        return {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": items,
        }

    def test_a_history_of_many_medium_outputs_is_trimmed_by_size(self) -> None:
        """A light-output history must not replay whole.

        Eleven snapshots once held 72% of one session's replayed tool output.
        That history had few, heavy outputs, so a count of kept outputs bounded
        it.  A thread whose outputs are many and medium -- forty-odd tool calls
        of a few kilobytes each -- stayed under that count on every turn, so
        nothing was elided at all and the upstream answered the request with
        its incomplete-tool-record rejection no matter what the client did.
        """

        hooks, _ = load_hook_module()
        # A snapshot-sized payload, so the default floor is not what decides.
        self.set_env(hooks._HISTORY_TOOL_OUTPUT_KEPT_CHARS_ENV, "60000")
        request = self._request(pairs=30, payload_chars=30_000)

        updated = hooks._with_elided_oversized_tool_outputs(request)

        self.assertIsNotNone(updated)
        assert updated is not None
        outputs = [
            item
            for item in updated["input"]
            if item["type"] == "function_call_output"
        ]
        kept = [item for item in outputs if item["output"] == "x" * 30_000]
        self.assertLessEqual(len(kept) * 30_000, 60_000)
        self.assertEqual(len(kept), 2)
        elided = [item for item in outputs if item["output"] != "x" * 30_000]
        self.assertTrue(elided)
        self.assertIn("elided", elided[0]["output"][0]["text"])
        # The newest record survives whole, so the model still works from it.
        self.assertEqual(outputs[-1]["output"], "x" * 30_000)

    def test_a_dropped_output_keeps_its_call_arguments_verbatim(self) -> None:
        """An emptied record leaves the call that introduced it untouched.

        The replay is the model's own few-shot example of how to call a tool.
        Rewriting an emptied ``view_image`` call's arguments into
        ``{"elided": "[N characters elided: ...]"}`` therefore taught the
        model to emit that exact shape: Codex's router answered
        ``failed to parse function arguments: missing field `path``` on 233
        calls in one thread, because the placeholder replays every turn.  The
        output note already says the payload is gone, so the call keeps the
        arguments it was issued with and the pair stays complete.
        """

        hooks, _ = load_hook_module()
        self.set_env(hooks._HISTORY_TOOL_OUTPUT_KEPT_CHARS_ENV, "60_000")
        request = self._request(pairs=6, payload_chars=30_000)

        updated = hooks._with_elided_oversized_tool_outputs(request)

        self.assertIsNotNone(updated)
        assert updated is not None
        items = updated["input"]
        elided_calls = 0
        for index, item in enumerate(items):
            if item["type"] != "function_call":
                continue
            output = items[index + 1]
            self.assertEqual(output["type"], "function_call_output")
            # Every call still names its own arguments, elided or not.
            self.assertEqual(item["arguments"], '{"cmd":"ls"}')
            self.assertEqual(item["call_id"], output["call_id"])
            if output["output"] == "x" * 30_000:
                continue
            elided_calls += 1
            # The note belongs to the output only.
            self.assertIn("elided", output["output"][0]["text"])
        self.assertTrue(elided_calls)

    def test_a_view_image_output_is_elided_without_breaking_its_call(self) -> None:
        """The ``view_image`` shape that poisoned a real Codex thread.

        Its call carries ``{"path": ..., "detail": ...}`` and its output is
        one inline image.  The elision replaced the output with its note and,
        in the same pass, replaced the call's arguments with the note object,
        so the replayed call no longer parsed as a ``view_image`` call and the
        model copied the broken shape on the next turn.  The call must keep its
        path and detail exactly as issued.
        """

        hooks, _ = load_hook_module()
        # The siblings stay under the per-record floor, so the oversized image
        # is the one record this window empties.
        self.set_env(hooks._HISTORY_TOOL_OUTPUT_KEPT_CHARS_ENV, "60_000")
        arguments = '{"path":"/tmp/shot.png","detail":"original"}'
        request = {
            "call_type": "aresponses",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "function_call",
                    "id": "fc_view",
                    "name": "view_image",
                    "namespace": "functions",
                    "arguments": arguments,
                    "call_id": "call_view",
                },
                {
                    "type": "function_call_output",
                    "id": "fco_view",
                    "call_id": "call_view",
                    "output": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64," + ("C" * 150_000),
                        }
                    ],
                },
            ]
            # Sibling records stay under the per-record floor, so the oversized
            # image is the one record this window empties.
            + sum(
                (
                    [
                        {
                            "type": "function_call",
                            "id": f"fc_{index}",
                            "name": "exec_command",
                            "namespace": "functions",
                            "arguments": '{"cmd":"ls"}',
                            "call_id": f"call_{index}",
                        },
                        {
                            "type": "function_call_output",
                            "id": f"fco_{index}",
                            "call_id": f"call_{index}",
                            "output": "x" * 15_000,
                        },
                    ]
                    for index in range(6)
                ),
                [],
            ),
        }

        updated = hooks._with_elided_oversized_tool_outputs(request)

        self.assertIsNotNone(updated)
        assert updated is not None
        view_call = updated["input"][0]
        self.assertEqual(view_call["arguments"], arguments)
        self.assertEqual(view_call["name"], "view_image")
        # Its own output still carries the note rather than the image bytes.
        output = updated["input"][1]["output"]
        self.assertIn("elided", output[0]["text"])
        self.assertNotIn("data:image/png", json.dumps(output, ensure_ascii=False))

    def test_no_elided_placeholder_reaches_a_replayed_call(self) -> None:
        """Nothing this pass writes into a call may confuse the tool schema."""

        hooks, _ = load_hook_module()
        self.set_env(hooks._HISTORY_TOOL_OUTPUT_KEPT_CHARS_ENV, "60_000")
        request = self._request(pairs=8, payload_chars=30_000)

        updated = hooks._with_elided_oversized_tool_outputs(request)

        self.assertIsNotNone(updated)
        assert updated is not None
        for item in updated["input"]:
            if not item["type"].endswith("_call"):
                continue
            self.assertNotIn("elided", item.get("arguments") or "")
            self.assertNotIn("characters elided", item.get("input") or "")

    def test_a_history_that_fits_is_left_byte_for_byte(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._HISTORY_TOOL_OUTPUT_KEPT_CHARS_ENV, "1000000")
        request = self._request(pairs=5, payload_chars=30_000)

        self.assertIsNone(hooks._with_elided_oversized_tool_outputs(request))

    def test_the_ceiling_is_the_routes_own_window(self) -> None:
        """Not a fixed character count: 1M tokens at 95%, less the answer's reserve.

        The route this app serves most often reports 1,000,000 tokens and 95%
        usable, and a client asks for 393,216 of them for its own answer.  What
        is left is the whole tail this pass may replay, and it is 84x the
        20,000 a fixed window allowed -- which is why that window emptied every
        read the model had made instead of guarding a ceiling.
        """

        hooks, _ = load_hook_module()
        module = hooks._responses_request_module

        ceiling = module._history_tool_output_ceiling_chars_for_window(
            context_window=1_000_000,
            max_context_window=1_000_000,
            effective_percent=95,
            max_output_tokens=393_216,
        )

        self.assertEqual((950_000 - 393_216) * 3, ceiling)
        self.assertGreater(ceiling, 20_000 * 80)

    def test_an_output_reserve_cannot_claim_the_whole_window(self) -> None:
        """A client's own maximum never removes the ceiling."""

        hooks, _ = load_hook_module()
        module = hooks._responses_request_module

        ceiling = module._history_tool_output_ceiling_chars_for_window(
            context_window=200_000,
            max_context_window=None,
            effective_percent=100,
            max_output_tokens=5_000_000,
        )

        self.assertEqual(100_000 * 3, ceiling)

    def test_a_tail_the_route_can_carry_is_not_a_snapshot(self) -> None:
        """The session that motivated this pass: 630k of output on a 1M route.

        Eleven of thirteen live histories hit the old fixed window, and every
        one of them lost its entire set of records above the drop threshold --
        including the repository guide the model had just read.
        """

        hooks, _ = load_hook_module()
        request = self._request(pairs=10, payload_chars=63_000)

        with mock.patch.object(
            hooks._responses_request_module,
            "_history_tool_output_ceiling_chars",
            return_value=1_670_352,
        ):
            with self.assertNoLogs("young_router.route_trace", level="WARNING"):
                updated = hooks._with_elided_oversized_tool_outputs(request)

        self.assertIsNone(updated)



class HookReplayedReasoningCountTests(HookTestCase):
    """The pre-call estimate must read a replayed item as its upstream does."""

    def test_an_unsigned_reasoning_item_counts_by_identity(self) -> None:
        """Its summary text is billed as a message and sent by nobody.

        Codex replays prior thinking as an opaque id: the client never sends the
        summary words back, and the upstream that wrote them is the only party
        that can read them.  Counting them as chat content made LiteLLM charge
        1,004,863 tokens for a 400k prompt and refuse a turn the route could
        serve.
        """

        hooks, _ = load_hook_module()
        items = [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "x" * 40_000}],
                "content": None,
                "encrypted_content": None,
            }
        ]

        counted = hooks._without_replayed_reasoning_summary_text(items)

        self.assertEqual(counted[0]["id"], "rs_1")
        self.assertNotIn("summary", counted[0])
        # The request itself is untouched; only the value counted is stripped.
        self.assertEqual(items[0]["summary"][0]["text"], "x" * 40_000)

    def test_a_reasoning_item_with_visible_content_keeps_it(self) -> None:
        """Only summary-only thinking is free; text a client can send is not."""

        hooks, _ = load_hook_module()
        item = {
            "type": "reasoning",
            "id": "rs_3",
            "summary": [{"type": "summary_text", "text": "summary"}],
            "content": [{"type": "reasoning_text", "text": "visible"}],
        }

        counted = hooks._without_replayed_reasoning_summary_text([item])

        self.assertIs(counted[0], item)

    def test_a_signed_reasoning_item_keeps_its_bytes_but_not_its_prose(self) -> None:
        """The signature is the reasoning; the summary is only a rendering of it.

        A replayed item carrying ``encrypted_content`` also carries the summary
        text of the thinking it encodes.  The counter prices that prose as chat
        content, which no upstream ever receives as such, so a signed item was
        charged for text that is merely a human rendering of bytes it already
        has.  That is how a thread the client measured at 647k reached
        1,002,856 against a 1,000,000 window with only 0.3% of the overage
        real.  The signature itself is kept: it is what gets replayed.
        """

        hooks, _ = load_hook_module()
        item = {
            "type": "reasoning",
            "id": "rs_2",
            "summary": [{"type": "summary_text", "text": "kept"}],
            "encrypted_content": "gAAAAAB-signed-prefix",
        }

        counted = hooks._without_replayed_reasoning_summary_text([item])

        self.assertIn("encrypted_content", counted[0])
        self.assertNotIn("summary", counted[0])
        # The request itself is untouched.
        self.assertIn("summary", item)

    def test_every_other_input_item_is_counted_unchanged(self) -> None:
        hooks, _ = load_hook_module()
        items = [
            {"type": "message", "role": "user", "content": "hello"},
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "y" * 100,
            },
        ]

        counted = hooks._without_replayed_reasoning_summary_text(items)

        self.assertEqual(counted, items)
        self.assertIs(counted[0], items[0])

    def test_a_string_input_is_not_reinterpreted(self) -> None:
        hooks, _ = load_hook_module()

        self.assertEqual(
            hooks._without_replayed_reasoning_summary_text("hello"), "hello"
        )


class HookInternalRequestKeyTests(HookTestCase):
    """Routing bookkeeping must never reach a provider's payload validator.

    ``_young_router_upstream_url_surface`` and friends ride on a request so a
    retry can tell which deployment was already tried.  LiteLLM forwards
    request kwargs to a provider largely as-is, and the Anthropic adapter
    rejects unknown fields, so one of these keys in the payload fails the call
    outright: "_young_router_upstream_url_surface: extra inputs are not
    permitted".  That is the fallback path -- the one already recovering from a
    failure -- so it turned a recoverable miss into a 400.
    """

    def test_every_internal_key_is_stripped(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "model": "claude-opus-5-5",
            "input": [{"role": "user", "content": "hi"}],
            "_young_router_upstream_url_surface": "openai_chat",
            "_young_router_attempted_upstream_url_surfaces": ["openai_chat"],
            "_young_router_surface_target_deployment_id": "b6015391",
            "_target_order": 1.2,
            "_excluded_deployment_ids": ["b6015391"],
        }

        stripped = hooks._without_internal_request_keys(request)

        self.assertEqual(sorted(stripped), ["input", "model"])
        # The request the rest of the pipeline still holds is untouched.
        self.assertIn("_target_order", request)

    def test_a_request_without_internal_keys_is_left_alone(self) -> None:
        hooks, _ = load_hook_module()

        self.assertIsNone(hooks._without_internal_request_keys({"model": "x"}))

    def test_a_client_field_that_merely_starts_with_an_underscore_survives(self) -> None:
        hooks, _ = load_hook_module()
        request = {"model": "x", "_custom_client_field": "keep"}

        self.assertIsNone(hooks._without_internal_request_keys(request))
