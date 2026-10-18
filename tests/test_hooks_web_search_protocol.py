from __future__ import annotations

import copy

from hook_test_utils import *


class HookWebSearchProtocolTests(HookTestCase):
    @staticmethod
    def search_response(call_id="lookup_1", name="web_search", arguments=None):
        return {
            "id": "resp_" + call_id,
            "status": "completed",
            "output": [
                {"type": "reasoning", "id": "rs_" + call_id, "summary": [], "encrypted_content": "opaque"},
                {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id,
                 "name": name, "arguments": json.dumps(arguments or {"query": "sample"}), "status": "completed"},
            ],
        }

    async def test_two_rounds_preserve_task_reasoning_and_client_call_at_round_limit(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                hooks, _ = load_hook_module()
                owner = hooks._owners["_resolve_web_search_function_calls"]
                original_action = owner._external_web_search_run_action

                async def run_action(action, *_args):
                    section = "search evidence" if action["type"] == "search" else "page evidence"
                    return section, ["https://example.test/source"], action

                owner._external_web_search_run_action = run_action
                self.addCleanup(setattr, owner, "_external_web_search_run_action", original_action)
                calls = []
                final = {"id": "resp_client", "status": "completed", "output": [
                    {"type": "function_call", "id": "fc_save", "call_id": "save_1", "name": "save_report",
                     "arguments": '{"text":"verified"}', "status": "completed"},
                ]}

                async def upstream(**kwargs):
                    calls.append(copy.deepcopy(kwargs))
                    if len(calls) == 1:
                        return self.search_response("lookup_2", "fetch_content", {"url": "https://example.test/source"})
                    return copy.deepcopy(final)

                original_input = [{"role": "developer", "content": "Research, save the report, then finish."},
                                  {"role": "user", "content": "Complete the report."}]
                request = {"model": "sample-chat", "input": copy.deepcopy(original_input),
                           "instructions": "Keep all task requirements.", "reasoning": {"effort": "high"},
                           "tools": [{"type": "web_search"}, {"type": "function", "name": "save_report", "parameters": {"type": "object"}}],
                           "stream": stream}
                self.set_env("YOUNG_ROUTER_WEB_SEARCH_MAX_ROUNDS", "2")
                if stream:
                    chunks = [jsonable_stream_chunk(chunk) async for chunk in owner._resolve_web_search_function_calls_stream_rounds(
                        self.search_response(), request, upstream)]
                    result = chunks[-1]["response"]
                else:
                    result = await owner._resolve_web_search_function_calls(self.search_response(), request, upstream)
                self.assertEqual(result["output"][-1], final["output"][0])
                self.assertEqual(len(calls), 2)
                for call in calls:
                    self.assertEqual(call["input"][:2], original_input)
                    self.assertEqual(call["instructions"], request["instructions"])
                    self.assertEqual(call["reasoning"], {"effort": "high"})
                    self.assertIn("save_report", json.dumps(call["tools"]))
                outputs = [item for item in calls[-1]["input"] if item.get("type") == "function_call_output"]
                self.assertEqual([(item["call_id"], item["output"]) for item in outputs],
                                 [("lookup_1", "search evidence"), ("lookup_2", "page evidence")])
                self.assertEqual([item["encrypted_content"] for item in calls[-1]["input"] if item.get("type") == "reasoning"],
                                 ["opaque", "opaque"])

    async def test_progress_prose_and_phase_do_not_trigger_an_extra_request(self):
        hooks, _ = load_hook_module()
        calls = []
        response = {"id": "resp_progress", "status": "completed", "end_turn": False, "output": [
            {"type": "message", "role": "assistant", "phase": "commentary", "status": "completed",
             "content": [{"type": "output_text", "text": "I will continue researching."}]},
        ]}

        async def upstream(**kwargs):
            calls.append(kwargs)
            return copy.deepcopy(response)

        result = await hooks._external_web_search_continue_or_synthesize(
            request_kwargs={"model": "sample-chat", "input": "Finish the task."},
            response=self.search_response(), search_results="evidence", queries=["sample"],
            source_urls=[], round_number=1, original_function=upstream)
        self.assertEqual(result, response)
        self.assertEqual(len(calls), 1)

    async def test_empty_or_failed_continuation_keeps_request_and_never_forces_final(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                hooks, _ = load_hook_module()
                calls = []
                error = RuntimeError("upstream failure")
                self.set_env("YOUNG_ROUTER_EXTERNAL_WEB_SEARCH_MODEL_RETRIES", "0")

                async def upstream(**kwargs):
                    calls.append(copy.deepcopy(kwargs))
                    if failure:
                        raise error
                    return {"id": "empty", "status": "completed", "output": []}

                with self.assertRaises(RuntimeError) as raised:
                    await hooks._external_web_search_continue_or_synthesize(
                        request_kwargs={"model": "sample-chat", "input": "Finish and save.", "instructions": "Task contract."},
                        response=self.search_response(), search_results="retrieved evidence", queries=["sample"],
                        source_urls=[], round_number=1, original_function=upstream)
                self.assertEqual(len(calls), 1)
                recovery = hooks._external_web_search_recovery_request_from_exception(raised.exception)
                self.assertEqual(recovery["input"], calls[0]["input"])
                self.assertEqual(recovery["tools"], calls[0]["tools"])
                self.assertEqual(recovery["instructions"], "Task contract.")
                self.assertNotIn("external_web_search_synthesis", recovery["litellm_metadata"])

    def test_each_parallel_search_call_receives_its_own_result(self):
        hooks, _ = load_hook_module()
        request = {"model": "sample-chat", "input": "Compare both sources."}
        hooks._external_web_search_remember_action_outputs(request, [
            ({"type": "search", "query": "first"}, "first result"),
            ({"type": "search", "query": "second"}, "second result"),
        ])
        response = self.search_response("first_call", arguments={"query": "first"})
        response["output"].extend(self.search_response("second_call", arguments={"query": "second"})["output"])
        continuation = hooks._external_web_search_continuation_kwargs(
            request, response=response, search_results="first result\n\nsecond result", queries=["first", "second"], round_number=1)
        outputs = [item for item in continuation["input"] if item.get("type") == "function_call_output"]
        self.assertEqual([(item["call_id"], item["output"]) for item in outputs],
                         [("first_call", "first result"), ("second_call", "second result")])

    def test_one_multi_query_call_returns_one_aggregated_output_with_its_call_id(self):
        hooks, _ = load_hook_module()
        arguments = {
            "queries": ["first", "second"],
            "numResults": 3,
            "provider": "pi-web-access",
        }
        actions = hooks._web_search_actions_from_call({
            "type": "function_call",
            "name": "web_search",
            "call_id": "batch_call",
            "arguments": json.dumps(arguments),
        })
        request = {"model": "sample-chat", "input": "Compare both sources."}
        hooks._external_web_search_remember_action_outputs(request, [
            (actions[0], "first result"),
            (actions[1], "second result"),
        ])
        response = self.search_response("batch_call", arguments=arguments)
        continuation = hooks._external_web_search_continuation_kwargs(
            request, response=response, search_results="first result\n\nsecond result",
            queries=["first", "second"], round_number=1)
        outputs = [item for item in continuation["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(outputs, [{
            "type": "function_call_output",
            "call_id": "batch_call",
            "output": "first result\n\nsecond result",
        }])

    def test_completed_action_metadata_keeps_options_for_deduplication(self):
        hooks, _ = load_hook_module()
        action = {
            "type": "search",
            "query": "latest Python",
            "numResults": "3",
            "includeContent": "true",
            "domainFilter": "[\"example.test\"]",
            "provider": "pi-web-access",
        }
        request = {
            "litellm_metadata": {
                "external_web_search_completed_actions": [copy.deepcopy(action)],
            },
        }
        completed = hooks._external_web_search_completed_actions_metadata(request)
        self.assertEqual(completed, [action])
        self.assertEqual(hooks._external_web_search_budgeted_actions([action], completed), [])

    async def test_search_budget_exhaustion_is_an_error_with_no_forced_answer(self):
        hooks, _ = load_hook_module()
        with self.assertRaises(RuntimeError) as raised:
            await hooks._external_web_search_finalize_response(
                self.search_response(), request_kwargs={"model": "sample-chat", "input": "Keep investigating."},
                search_results="evidence", queries=["sample"], source_urls=[], original_function=None)
        self.assertIn("web_search_action_budget_exhausted", str(raised.exception))

    async def test_inline_tool_tag_explanation_is_not_truncated(self):
        hooks, _ = load_hook_module()
        text = "The filter only knows " + chr(96) + "<tool_call>" + chr(96) + "; this is an explanation."
        text_filter = hooks._RawToolCallTextFilter()
        self.assertEqual("".join(text_filter.consume(char) for char in text), text)
        self.assertEqual(hooks._strip_raw_tool_call_blocks(text), text)
        self.assertEqual(hooks._strip_raw_tool_call_blocks("."), ".")

    def test_fenced_dsml_example_is_not_a_tool_call(self):
        hooks, _ = load_hook_module()
        marker = "\uff5c\uff5cDSML\uff5c\uff5c"
        raw = "<" + marker + ' calls>\n<' + marker + ' invoke name="save_report">\n</' + marker + ' invoke>\n</' + marker + ' calls>'
        text = chr(96) * 3 + "xml\n" + raw + "\n" + chr(96) * 3
        self.assertEqual(hooks._raw_dsml_tool_call_items(text), [])
        self.assertEqual(hooks._strip_raw_tool_call_blocks(text), text)
