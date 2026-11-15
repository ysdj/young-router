from __future__ import annotations

from unittest import mock

from hook_test_utils import *


def _deployment(route: str) -> dict:
    return {
        "litellm_params": {"model": f"openai/{route}", "order": 1},
        "model_info": {"id": route, "order": 1},
    }


def _request(turn: str, route: str) -> dict:
    return {
        "model": "default-chat",
        "stream": True,
        "input": [{"role": "user", "content": "hello"}],
        "_young_router_attempt_lease_id": turn,
        "litellm_params": {"model": f"openai/{route}", "order": 1},
        "model_info": {"id": route, "order": 1},
    }


def _saturate(hooks, deployments: list[dict]) -> None:
    for index, deployment in enumerate(deployments):
        assert hooks._claim_deployment_concurrency_slot(
            _request(f"busy-{index}", deployment["model_info"]["id"]),
            deployment,
            1,
        )


class _Router:
    def __init__(self, deployments: list[dict]) -> None:
        self.deployments = deployments

    def _get_all_deployments(self, model_name, team_id=None):
        return list(self.deployments)


class _MarkedStream:
    def __init__(self, marker: dict) -> None:
        self._young_router_selected_deployment_marker = marker
        self._iterator = self._chunks()

    async def _chunks(self):
        yield {"id": "chatcmpl-1", "choices": [{"index": 0, "delta": {"content": "ok"}}]}

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._iterator.__anext__()


class ResponsesExecutionEdgeTests(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.set_env(
            "YOUNG_ROUTER_DEPLOYMENT_CONCURRENCY_FILE",
            str(Path(self.directory.name) / "deployment-concurrency.json"),
        )

    def test_all_saturated_candidates_are_reported(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        deployments = [_deployment("route-a"), _deployment("route-b")]
        _saturate(hooks, deployments)
        request = _request("new", "route-a")
        error = RuntimeError("ServiceUnavailableError: upstream 503")
        error.status_code = 503
        error.failed_deployment_id = "route-a"
        error.failed_deployment_order = 1
        with mock.patch.object(
            hooks._responses_execution_module._trace_module,
            "_request_route_trace",
        ) as trace:
            hooks._ordered_deployment_fallback_entry(
                _Router(deployments), error, request
            )
        event = next(
            call for call in trace.call_args_list
            if call.args[0] == "fallback_model_concurrency_filter"
        )
        self.assertTrue(event.kwargs["concurrency_all_candidates"])

    async def test_the_image_sanitizer_keeps_the_selected_deployment_marker(self) -> None:
        hooks, _ = load_hook_module()
        marker = {"deployment_id": "route-a"}
        original_function = lambda **kwargs: _MarkedStream(marker)
        request = {
            "model": "default-chat",
            "input": [{"type": "input_image", "image_url": "data:image/png;base64,abc"}],
        }
        response = await hooks._responses_execution_module._execute_responses_chat_bridge_call(
            original_function,
            request,
            original_request_kwargs=request,
            start_event="bridge_start",
            error_event="bridge_error",
        )
        self.assertIs(
            hooks._selected_deployment_marker_from_response(response),
            marker,
        )


if __name__ == "__main__":
    unittest.main()
