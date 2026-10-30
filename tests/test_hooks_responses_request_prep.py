from __future__ import annotations

import base64
import json
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
            "litellm_params": {"model": "anthropic/claude-opus-5-5"},
        }

        stripped = hooks._without_internal_request_keys(request)

        self.assertEqual(sorted(stripped), ["input", "model"])
        # The request the rest of the pipeline still holds is untouched.
        self.assertIn("_target_order", request)

    def test_the_routing_container_is_stripped_but_api_base_survives(self) -> None:
        """``litellm_params`` is a routing container, not a provider parameter.

        This proxy builds it to carry ``api_base``/``model`` while choosing a
        route, and copies whatever the provider needs onto the request itself.
        Leaving it on the payload failed every fallback to an Anthropic route
        with "litellm_params: extra inputs are not permitted" even after the
        surface keys were removed.
        """

        hooks, _ = load_hook_module()
        request = {
            "model": "claude-opus-5-5",
            "input": "hi",
            "api_base": "https://api.sisct2.xyz",
            "litellm_params": {"api_base": "https://api.sisct2.xyz"},
        }

        stripped = hooks._without_internal_request_keys(request)

        self.assertEqual(sorted(stripped), ["api_base", "input", "model"])

    def test_a_request_without_internal_keys_is_left_alone(self) -> None:
        hooks, _ = load_hook_module()

        self.assertIsNone(hooks._without_internal_request_keys({"model": "x"}))

    def test_a_client_field_that_merely_starts_with_an_underscore_survives(self) -> None:
        hooks, _ = load_hook_module()
        request = {"model": "x", "_custom_client_field": "keep"}

        self.assertIsNone(hooks._without_internal_request_keys(request))
