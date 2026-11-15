from __future__ import annotations

from unittest import mock

from hook_test_utils import *


class HookFilterDeploymentTraceEdgeTests(HookTestCase):
    def capture_filter_trace(self, hooks):
        """Record the fields of each ``filter_deployments`` trace event."""

        traced: list[dict] = []
        original = hooks._route_trace
        hooks._route_trace = lambda event, **fields: (
            traced.append(fields) if event == "filter_deployments" else None
        )
        self.addCleanup(setattr, hooks, "_route_trace", original)
        return traced

    @staticmethod
    def image_input_request() -> dict:
        return {
            "call_type": "aresponses",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "read this"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,abc",
                        },
                    ],
                }
            ],
        }

    @staticmethod
    def deployment(deployment_id: str, *, allows_image_input: bool = True) -> dict:
        model_info: dict = {"id": deployment_id, "supports_vision": True}
        if not allows_image_input:
            model_info["supports_responses_image_input"] = False
        return {
            "litellm_params": {"api_base": f"https://{deployment_id}.example/v1"},
            "model_info": model_info,
        }

    async def test_filter_trace_reports_no_image_input_filtering_for_a_fully_safe_pool(self) -> None:
        hooks, _ = load_hook_module()
        traced = self.capture_filter_trace(hooks)
        deployments = [self.deployment("trace-safe-a"), self.deployment("trace-safe-b")]

        await hooks.YoungRouterHook().async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs=self.image_input_request(),
        )

        self.assertFalse(traced[0]["responses_image_input_filtered"])

    async def test_filter_trace_reports_image_input_filtering_when_no_route_allows_it(self) -> None:
        hooks, _ = load_hook_module()
        traced = self.capture_filter_trace(hooks)
        deployments = [
            self.deployment("trace-unsafe-a", allows_image_input=False),
            self.deployment("trace-unsafe-b", allows_image_input=False),
        ]

        filtered = await hooks.YoungRouterHook().async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs=self.image_input_request(),
        )

        self.assertTrue(traced[0]["responses_image_input_filtered"])
        self.assertEqual(filtered, deployments)


class FailureCallbackSlotReleaseTests(HookTestCase):
    """One ending gives a turn's slot back once.

    The failure callback releases the slot itself; a second release after it
    finds nothing held and is silent, so it can only ever be dead code.
    """

    @staticmethod
    def failed_request() -> dict:
        return {
            "model": "default-chat",
            "stream": False,
            "input": [{"role": "user", "content": "hello"}],
            "_young_router_attempt_lease_id": "turn-failure",
            "litellm_params": {"model": "openai/route-a", "order": 1},
            "model_info": {"id": "route-a", "order": 1},
        }

    async def test_a_failed_turn_releases_its_slot_exactly_once(self) -> None:
        hooks, _ = load_hook_module()
        with mock.patch.object(
            hooks._routing_module, "_release_request_concurrency_slots"
        ) as release:
            await hooks.YoungRouterHook().async_log_failure_event(
                kwargs=self.failed_request(),
                response_obj=RuntimeError("upstream refused"),
                start_time=None,
                end_time=None,
            )

        self.assertEqual(1, release.call_count)
