from __future__ import annotations

import types
from unittest import mock

from hook_test_utils import *


class _PromptTokensDetails:
    def __init__(self, cached_tokens: int = 0) -> None:
        self.cached_tokens = cached_tokens


class _Usage:
    def __init__(self) -> None:
        self.prompt_tokens = 16_224
        self.prompt_cache_hit_tokens = 16_000
        self.prompt_tokens_details = _PromptTokensDetails()


class _TransformConfig:
    @staticmethod
    def transform_chat_completion_response_to_responses_api_response(*args, **kwargs):
        return "transformed"


def _install_fake_litellm_modules(case: unittest.TestCase) -> None:
    names = (
        "litellm.responses",
        "litellm.responses.litellm_completion_transformation",
        "litellm.responses.litellm_completion_transformation.transformation",
        "litellm.types",
        "litellm.types.utils",
    )
    modules = {name: types.ModuleType(name) for name in names}
    modules[
        "litellm.responses.litellm_completion_transformation.transformation"
    ].LiteLLMCompletionResponsesConfig = _TransformConfig
    modules["litellm.types.utils"].PromptTokensDetailsWrapper = _PromptTokensDetails
    for name, module in modules.items():
        previous = sys.modules.get(name)
        sys.modules[name] = module
        if previous is None:
            case.addCleanup(sys.modules.pop, name, None)
        else:
            case.addCleanup(sys.modules.__setitem__, name, previous)


_DEPLOYMENTS = [
    {
        "litellm_params": {"model": "openai/default-chat", "order": 1},
        "model_info": {"id": "route-a", "order": 1},
    },
    {
        "litellm_params": {"model": "openai/default-chat", "order": 2},
        "model_info": {"id": "route-b", "order": 2},
    },
]
_REFUSAL = (
    "litellm.BadRequestError: OpenAIException - thinking mode does not support "
    "this tool_choice"
)


class _BadRequest(Exception):
    status_code = 400


class _OrderFallbackRouter:
    def _get_all_deployments(self, model_name, team_id=None):
        return _DEPLOYMENTS

    async def _ageneric_api_call_with_fallbacks_helper(
        self, model, original_generic_function, **kwargs
    ):
        excluded = set(kwargs.get("_excluded_deployment_ids") or ())
        target_order = kwargs.get("_target_order")
        selected = next(
            deployment
            for deployment in _DEPLOYMENTS
            if deployment["model_info"]["id"] not in excluded
            and (
                target_order is None
                or deployment["litellm_params"]["order"] == target_order
            )
        )
        selected_kwargs = kwargs.copy()
        selected_kwargs["model_info"] = selected["model_info"].copy()
        selected_kwargs["litellm_params"] = selected["litellm_params"].copy()
        return await original_generic_function(**selected_kwargs)


def _refusal(route: str, order: int) -> Exception:
    error = _BadRequest(_REFUSAL)
    error.failed_deployment_id = route
    error.failed_deployment_order = order
    return error


class ProxyPatchEdgeTests(HookTestCase):
    def test_cache_usage_trace_reports_the_repair_and_its_before_value(self) -> None:
        hooks, _ = load_hook_module()
        _install_fake_litellm_modules(self)
        hooks._install_responses_bridge_cache_usage_patch()
        usage = _Usage()
        events: list[dict] = []
        with mock.patch.object(
            hooks._trace_module,
            "_route_trace",
            lambda name, **fields: events.append(fields),
        ):
            result = _TransformConfig.transform_chat_completion_response_to_responses_api_response(
                request_input=[],
                chat_completion_response=types.SimpleNamespace(usage=usage),
                responses_api_request={},
            )
        self.assertEqual("transformed", result)
        self.assertEqual(0, events[0]["cached_tokens_before"])
        self.assertIs(True, events[0]["repaired"])
        self.assertEqual(16_000, usage.prompt_tokens_details.cached_tokens)


class SelectedDeploymentTraceTests(HookTestCase):
    def _install(self):
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")

        class Router:
            def _update_kwargs_with_deployment(self, deployment, kwargs, function_name=None):
                kwargs["litellm_params"] = dict(deployment["litellm_params"])

        router_module.Router = Router
        self.addCleanup(sys.modules.pop, "litellm.router", None)
        sys.modules["litellm.router"] = router_module
        hooks._install_selected_deployment_marker_patch()
        return hooks, Router

    def test_a_route_that_needs_no_surface_still_records_its_selection(self) -> None:
        hooks, Router = self._install()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "0")
        deployment = _DEPLOYMENTS[0]
        request = {
            "model": "default-chat",
            "litellm_params": dict(deployment["litellm_params"]),
            "model_info": dict(deployment["model_info"]),
        }
        events: list[tuple[str, dict]] = []
        with mock.patch.object(
            hooks._trace_module,
            "_route_trace",
            lambda name, **fields: events.append((name, fields)),
        ):
            Router()._update_kwargs_with_deployment(deployment, request)
        selected = [fields for name, fields in events if name == "selected_deployment"]
        self.assertEqual(1, len(selected))
        self.assertEqual("route-a", selected[0]["deployment"]["id"])
        self.assertNotIn("surface", selected[0]["deployment"])
