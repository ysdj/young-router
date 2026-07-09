from __future__ import annotations

from hook_test_utils import *


class HookPatchTests(HookTestCase):
    def test_selected_deployment_update_preserves_original_route_model_group(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "Use web_search.",
        }

        hooks._remember_request_model_group_before_deployment_update(request_kwargs)
        request_kwargs["model"] = "openai/vendor-chat"

        synthesis_kwargs = hooks._external_web_search_synthesis_kwargs(
            request_kwargs,
            "Web search results for query: test\n"
            "Title: source\n"
            "URL: https://example.test\n"
            "Snippet: evidence",
        )

        self.assertEqual(
            request_kwargs["litellm_metadata"][
                hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY
            ],
            "balanced-chat",
        )
        self.assertEqual(synthesis_kwargs["model"], "balanced-chat")

    def test_selected_deployment_marker_does_not_create_clientside_credential_override(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "legacy-chat",
            "input": "Use web_search.",
        }
        deployment = {
            "model_name": "legacy-chat",
            "litellm_params": {
                "model": "openai/vendor-chat",
                "api_base": "https://chat-provider.example/v1",
                "api_key": "sk-test-route",
                "order": 1,
            },
            "model_info": {
                "id": "chat-route",
                "provider": "provider_chat",
                "api_key_name": "generic-chat",
            },
        }

        hooks._remember_selected_deployment_for_request(request_kwargs, deployment)

        self.assertNotIn("api_base", request_kwargs)
        self.assertNotIn("api_key", request_kwargs)
        self.assertEqual(request_kwargs["litellm_params"]["api_base"], "https://chat-provider.example/v1")
        self.assertNotIn("api_key", request_kwargs["litellm_params"])
        self.assertEqual(request_kwargs["litellm_metadata"]["api_base"], "https://chat-provider.example/v1")
        self.assertEqual(request_kwargs["model_info"]["model_group"], "legacy-chat")

    async def test_generic_helper_patch_wraps_bound_original_generic_function(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        seen = {}

        class Router:
            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                seen["wrapped"] = getattr(
                    original_generic_function,
                    hooks._GENERIC_HELPER_PATCH_ATTR,
                    False,
                )
                return await original_generic_function(model_info={"id": "picked-deployment"})

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        error = RuntimeError("plain 403")
        error.status_code = 403

        async def original_generic_function(**kwargs):
            raise error

        with self.assertRaises(Exception) as context:
            await Router()._ageneric_api_call_with_fallbacks_helper(
                "default-chat",
                original_generic_function,
            )

        sanitized = context.exception
        self.assertIs(sanitized.__cause__, error)
        self.assertEqual(getattr(sanitized, "status_code", None), 503)
        self.assertIn("upstream auth or balance error", str(sanitized))
        self.assertTrue(seen["wrapped"])
        self.assertEqual(error.failed_deployment_id, "picked-deployment")
        self.assertIsNone(getattr(error, "failed_deployment_order", None))
        self.assertEqual(error.num_retries, 0)

    async def test_generic_helper_patch_retries_image_unsupported_with_vision_bridge(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")

        class Router:
            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                kwargs = dict(kwargs)
                kwargs["model"] = "configured-upstream-model"
                return await original_generic_function(**kwargs)

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        seen = {"attempts": []}

        async def bridged_request_kwargs(request_kwargs):
            bridged = dict(request_kwargs)
            bridged["messages"] = [{"role": "user", "content": "vision text"}]
            bridged["litellm_metadata"] = {"vision_bridge_attempted": True}
            return bridged

        hooks.bridged_request_kwargs = bridged_request_kwargs

        async def original_generic_function(**kwargs):
            seen["attempts"].append(kwargs)
            if len(seen["attempts"]) == 1:
                error = RuntimeError("model does not support image input")
                error.status_code = 400
                raise error
            if "model" not in kwargs:
                raise TypeError("aresponses() missing 1 required positional argument: 'model'")
            return {"ok": True, "kwargs": kwargs}

        response = await Router()._ageneric_api_call_with_fallbacks_helper(
            "default-chat",
            original_generic_function,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "read"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ],
        )

        self.assertEqual({"ok": True, "kwargs": seen["attempts"][1]}, response)
        self.assertEqual("configured-upstream-model", seen["attempts"][1]["model"])
        self.assertEqual("vision text", seen["attempts"][1]["messages"][0]["content"])

    async def test_generic_helper_patch_preserves_next_order_constraints_across_final_retry(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        attempts = []

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {
                            "id": "backup_provider-x-plus",
                            "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
                        },
                    },
                    {
                        "litellm_params": {"order": 3},
                        "model_info": {
                            "id": "backup_provider-x-pro",
                            "route_key": "backup_provider / openai/default-chat / key=x-pro / order=3",
                        },
                    },
                ]

            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                attempts.append(kwargs.copy())
                if len(attempts) == 1:
                    exc = RuntimeError("upstream 502 from order 2")
                    exc.status_code = 502
                    exc.failed_deployment_id = "backup_provider-x-plus"
                    exc.failed_deployment_order = 2
                    exc.failed_deployment_route_key = (
                        "backup_provider / openai/default-chat / key=x-plus / order=2"
                    )
                    raise exc
                return "ok"

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        result = await Router()._ageneric_api_call_with_fallbacks_helper(
            "default-chat",
            lambda **kwargs: None,
            call_type="aresponses",
            input="compact",
            stream=True,
            model_info={
                "id": "backup_provider-x-plus",
                "order": 2,
                "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
            },
        )

        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("_target_order", attempts[0])
        self.assertEqual(attempts[1]["_target_order"], 3)
        self.assertEqual(attempts[1]["_excluded_deployment_ids"], ["backup_provider-x-plus"])

    def test_responses_tool_search_bridge_patch_restores_custom_tool_calls(self) -> None:
        hooks, _ = load_hook_module()
        responses_module = types.ModuleType("litellm.responses")
        bridge_module = types.ModuleType("litellm.responses.litellm_completion_transformation")
        transform_module = types.ModuleType(
            "litellm.responses.litellm_completion_transformation.transformation"
        )
        streaming_module = types.ModuleType(
            "litellm.responses.litellm_completion_transformation.streaming_iterator"
        )

        class LiteLLMCompletionResponsesConfig:
            @staticmethod
            def transform_chat_completion_response_to_responses_api_response(*args, **kwargs):
                return types.SimpleNamespace(
                    output=[
                        {
                            "type": "function_call",
                            "id": "call_patch",
                            "call_id": "call_patch",
                            "name": "apply_patch",
                            "arguments": '{"input":"*** Begin Patch"}',
                            "status": "completed",
                        }
                    ]
                )

        class LiteLLMCompletionStreamingIterator:
            def _queue_tool_call_delta_events(self):
                pass

            def _queue_final_tool_call_done_events(self):
                pass

        transform_module.LiteLLMCompletionResponsesConfig = LiteLLMCompletionResponsesConfig
        streaming_module.LiteLLMCompletionStreamingIterator = LiteLLMCompletionStreamingIterator
        sys.modules["litellm.responses"] = responses_module
        sys.modules["litellm.responses.litellm_completion_transformation"] = bridge_module
        sys.modules[
            "litellm.responses.litellm_completion_transformation.transformation"
        ] = transform_module
        sys.modules[
            "litellm.responses.litellm_completion_transformation.streaming_iterator"
        ] = streaming_module

        hooks._install_responses_tool_search_bridge_patch()

        response = LiteLLMCompletionResponsesConfig.transform_chat_completion_response_to_responses_api_response(
            [],
            {
                "tools": [
                    {
                        "type": "function",
                        "name": "apply_patch",
                        "parameters": {"type": "object"},
                        hooks._RESPONSES_BRIDGE_CUSTOM_TOOL_KEY: True,
                    }
                ]
            },
            {},
        )
        dumped = [hooks._jsonable(item) for item in response.output]

        self.assertEqual(dumped[0]["type"], "custom_tool_call")
        self.assertEqual(dumped[0]["name"], "apply_patch")
        self.assertEqual(dumped[0]["input"], "*** Begin Patch")
        self.assertNotIn("arguments", dumped[0])

    async def test_selected_deployment_marker_patch_marks_failed_deployment_order(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")

        class Router:
            def _update_kwargs_with_deployment(self, deployment, kwargs, function_name=None):
                kwargs["updated"] = True

            async def make_call(self, original_function, *args, **kwargs):
                response = original_function(*args, **kwargs)
                if hasattr(response, "__await__"):
                    return await response
                return response

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_selected_deployment_marker_patch()

        router = Router()
        error = RuntimeError("rate limited")
        error.status_code = 429
        deployment = {
            "litellm_params": {"order": 1},
            "model_info": {"id": "order1-a"},
        }

        async def original_function(**kwargs):
            router._update_kwargs_with_deployment(deployment, kwargs)
            raise error

        with self.assertRaises(RuntimeError):
            await router.make_call(original_function, model="runtime-model-alias")

        self.assertEqual(error.failed_deployment_id, "order1-a")
        self.assertEqual(error.failed_deployment_order, 1)
        self.assertEqual(error.num_retries, 0)

    async def test_selected_deployment_marker_defaults_blank_order_to_one(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")

        class Router:
            def _update_kwargs_with_deployment(self, deployment, kwargs, function_name=None):
                kwargs["updated"] = True

            async def make_call(self, original_function, *args, **kwargs):
                response = original_function(*args, **kwargs)
                if hasattr(response, "__await__"):
                    return await response
                return response

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_selected_deployment_marker_patch()

        router = Router()
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        deployment = {
            "litellm_params": {"order": ""},
            "model_info": {"id": "blank-order-a"},
        }

        async def original_function(**kwargs):
            router._update_kwargs_with_deployment(deployment, kwargs)
            raise error

        with self.assertRaises(RuntimeError):
            await router.make_call(original_function, model="runtime-model-alias")

        self.assertEqual(error.failed_deployment_id, "blank-order-a")
        self.assertEqual(error.failed_deployment_order, 1)
        self.assertEqual(error.num_retries, 0)

    async def test_selected_deployment_marker_defaults_missing_order_to_one(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")

        class Router:
            def _update_kwargs_with_deployment(self, deployment, kwargs, function_name=None):
                kwargs["updated"] = True

            async def make_call(self, original_function, *args, **kwargs):
                response = original_function(*args, **kwargs)
                if hasattr(response, "__await__"):
                    return await response
                return response

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_selected_deployment_marker_patch()

        router = Router()
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        deployment = {
            "litellm_params": {},
            "model_info": {"id": "missing-order-a"},
        }

        async def original_function(**kwargs):
            router._update_kwargs_with_deployment(deployment, kwargs)
            raise error

        with self.assertRaises(RuntimeError):
            await router.make_call(original_function, model="runtime-model-alias")

        self.assertEqual(error.failed_deployment_id, "missing-order-a")
        self.assertEqual(error.failed_deployment_order, 1)
        self.assertEqual(error.num_retries, 0)

    async def test_selected_deployment_marker_preserves_litellm_params_order_after_original_update(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")

        class Router:
            def _update_kwargs_with_deployment(self, deployment, kwargs, function_name=None):
                kwargs["model_info"] = deployment["model_info"].copy()
                kwargs["litellm_metadata"] = {
                    "model_info": deployment["model_info"].copy(),
                }

            async def make_call(self, original_function, *args, **kwargs):
                response = original_function(*args, **kwargs)
                if hasattr(response, "__await__"):
                    return await response
                return response

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_selected_deployment_marker_patch()

        router = Router()
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        deployment = {
            "litellm_params": {"model": "openai/gpt-image-2", "order": 2},
            "model_info": {
                "id": "image-order2",
                "route_key": "backup_provider / openai/gpt-image-2 / key=x-image / order=2",
            },
        }

        async def original_function(**kwargs):
            router._update_kwargs_with_deployment(deployment, kwargs)
            raise error

        with self.assertRaises(RuntimeError):
            await router.make_call(original_function, model="gpt-image-2")

        self.assertEqual(error.failed_deployment_id, "image-order2")
        self.assertEqual(error.failed_deployment_order, 2)
        self.assertEqual(error.num_retries, 0)

    async def test_public_image_generation_reuses_common_fallback_path(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        router_utils_module = types.ModuleType("litellm.router_utils")
        fallback_handlers_module = types.ModuleType(
            "litellm.router_utils.fallback_event_handlers"
        )
        seen = {"fallbacks": [], "original_common_called": False}

        async def run_async_fallback(*args, **kwargs):
            seen["fallbacks"].append(kwargs["fallback_model_group"])
            return "image-peer-response"

        fallback_handlers_module.run_async_fallback = run_async_fallback

        class Router:
            max_fallbacks = 8
            num_retries = 0

            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "x-pro"},
                    },
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "r-pro"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "x-image"},
                    },
                ]

            def _update_kwargs_with_deployment(self, deployment, kwargs, function_name=None):
                kwargs["updated"] = True

            async def make_call(self, original_function, *args, **kwargs):
                response = original_function(*args, **kwargs)
                if hasattr(response, "__await__"):
                    return await response
                return response

            async def async_function_with_retries(self, *args, **kwargs):
                original_function = kwargs.pop("original_function")
                return await self.make_call(original_function, *args, **kwargs)

            async def async_function_with_fallbacks(self, *args, **kwargs):
                model_group = kwargs.get("model")
                try:
                    return await self.async_function_with_retries(*args, **kwargs)
                except Exception as exc:
                    return await self.async_function_with_fallbacks_common_utils(
                        exc,
                        False,
                        None,
                        None,
                        None,
                        model_group,
                        args,
                        kwargs,
                    )

            async def async_function_with_fallbacks_common_utils(
                self,
                e,
                disable_fallbacks,
                fallbacks,
                context_window_fallbacks,
                content_policy_fallbacks,
                model_group,
                args,
                kwargs,
            ):
                seen["original_common_called"] = True
                raise e

            async def aimage_generation(self, prompt, model, **kwargs):
                kwargs["model"] = model
                kwargs["prompt"] = prompt
                kwargs["original_function"] = self._aimage_generation
                kwargs["num_retries"] = kwargs.get("num_retries", self.num_retries)
                kwargs.setdefault("metadata", {}).update({"model_group": model})
                return await self.async_function_with_fallbacks(**kwargs)

            async def _aimage_generation(self, prompt, model, **kwargs):
                deployment = {
                    "model_name": model,
                    "litellm_params": {
                        "model": "openai/gpt-image-2",
                        "api_base": "https://headers.example/v1",
                        "order": 1,
                    },
                    "model_info": {
                        "id": "x-pro",
                        "provider": "compat_provider",
                        "api_key_name": "x-pro",
                    },
                }
                self._update_kwargs_with_deployment(deployment, kwargs)
                error = RuntimeError("No available compatible accounts")
                error.status_code = 503
                raise error

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        sys.modules["litellm.router_utils"] = router_utils_module
        sys.modules["litellm.router_utils.fallback_event_handlers"] = fallback_handlers_module
        hooks._install_selected_deployment_marker_patch()
        hooks._install_order_peer_failover_patch()

        response = await Router().aimage_generation("draw", "gpt-image-2")

        self.assertEqual(response, "image-peer-response")
        self.assertFalse(seen["original_common_called"])
        self.assertEqual(
            seen["fallbacks"],
            [
                [
                    {
                        "model": "gpt-image-2",
                        "_target_order": 1,
                        "_excluded_deployment_ids": ["x-pro"],
                    }
                ]
            ],
        )

    def test_selected_deployment_marker_patch_applies_browser_headers_after_original_update(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")

        class Router:
            def _update_kwargs_with_deployment(self, deployment, kwargs, function_name=None):
                kwargs["litellm_metadata"] = {
                    "api_base": deployment["litellm_params"]["api_base"],
                    "model_info": deployment["model_info"].copy(),
                }
                kwargs["original_update_called"] = True

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module

        original_remember = hooks._remember_selected_deployment_for_request

        def noop_remember_selected_deployment_for_request(request_kwargs, deployment):
            return None

        hooks._remember_selected_deployment_for_request = (
            noop_remember_selected_deployment_for_request
        )
        self.addCleanup(
            setattr,
            hooks,
            "_remember_selected_deployment_for_request",
            original_remember,
        )

        hooks._install_selected_deployment_marker_patch()

        router = Router()
        deployment = {
            "litellm_params": {
                "api_base": "https://headers.example/v1",
                "model": "openai/gpt-image-2",
                "order": 1,
            },
            "model_info": {"id": "compat_provider-x-pro", "order": 1},
        }
        kwargs = {"extra_headers": {"X-Trace": "keep-me"}}

        router._update_kwargs_with_deployment(deployment, kwargs)

        self.assertTrue(kwargs["original_update_called"])
        self.assertEqual(kwargs["extra_headers"]["X-Trace"], "keep-me")
        self.assertIn("Mozilla/5.0", kwargs["extra_headers"]["User-Agent"])
        self.assertEqual(
            kwargs["extra_headers"]["Accept"], "application/json, text/plain, */*"
        )
        self.assertEqual(kwargs["litellm_metadata"]["api_base"], "https://headers.example/v1")

    def test_selected_deployment_marker_applies_browser_headers_from_retry_marker(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")

        class Router:
            def _update_kwargs_with_deployment(self, deployment, kwargs, function_name=None):
                kwargs["litellm_metadata"] = {
                    "api_base": deployment["litellm_params"]["api_base"],
                    "model_info": deployment["model_info"].copy(),
                }

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_selected_deployment_marker_patch()

        router = Router()
        deployment = {
            "litellm_params": {
                "api_base": "https://api.image.example/v1",
                "model": "openai/gpt-image-2",
                "order": 1,
            },
            "model_info": {
                "id": "image-provider-x-image",
            },
        }
        kwargs = {
            "call_type": "aimage_generation",
            "litellm_metadata": {
                hooks._BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY: True,
            },
            "extra_headers": {"User-Agent": "codex-local/1.2.3"},
        }

        router._update_kwargs_with_deployment(deployment, kwargs)

        self.assertIn("Mozilla/5.0", kwargs["extra_headers"]["User-Agent"])
        self.assertEqual(
            kwargs["extra_headers"]["Accept"], "application/json, text/plain, */*"
        )
        self.assertEqual(kwargs["extra_headers"]["Accept-Language"], "en-US,en;q=0.9")

    async def test_cloudflare_browser_signature_error_retries_with_browser_headers(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        router_utils_module = types.ModuleType("litellm.router_utils")
        fallback_handlers_module = types.ModuleType(
            "litellm.router_utils.fallback_event_handlers"
        )
        seen = {"fallback_kwargs": None, "original_called": False}

        async def run_async_fallback(*args, **kwargs):
            seen["fallback_kwargs"] = kwargs
            return "browser-retry-response"

        fallback_handlers_module.run_async_fallback = run_async_fallback

        class Router:
            max_fallbacks = 8

            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "browser-blocked-image"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "backup-x-image"},
                    },
                ]

            async def async_function_with_fallbacks_common_utils(
                self,
                e,
                disable_fallbacks,
                fallbacks,
                context_window_fallbacks,
                content_policy_fallbacks,
                model_group,
                args,
                kwargs,
            ):
                seen["original_called"] = True
                return "unexpected-original"

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        sys.modules["litellm.router_utils"] = router_utils_module
        sys.modules["litellm.router_utils.fallback_event_handlers"] = fallback_handlers_module
        hooks._install_order_peer_failover_patch()

        error = RuntimeError(
            "Cloudflare Error 1010: Access denied. The site owner has blocked access based on your browser's signature."
        )
        error.status_code = 403
        error.failed_deployment_id = "browser-blocked-image"
        error.failed_deployment_order = 1

        response = await Router().async_function_with_fallbacks_common_utils(
            error,
            False,
            None,
            None,
            None,
            "gpt-image-2",
            (),
            {
                "model": "gpt-image-2",
                "call_type": "aimage_generation",
                "metadata": {"model_group": "gpt-image-2"},
                "extra_headers": {"User-Agent": "codex-local/1.2.3"},
            },
        )

        self.assertEqual(response, "browser-retry-response")
        self.assertFalse(seen["original_called"])
        fallback_kwargs = seen["fallback_kwargs"]
        self.assertIsNotNone(fallback_kwargs)
        assert fallback_kwargs is not None
        self.assertIn("Mozilla/5.0", fallback_kwargs["extra_headers"]["User-Agent"])
        self.assertEqual(
            fallback_kwargs["fallback_model_group"],
            [{"model": "gpt-image-2", "_target_order": 1}],
        )
        self.assertNotIn("_excluded_deployment_ids", fallback_kwargs)

    async def test_make_call_does_not_mark_cloudflare_browser_signature_before_header_retry(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")

        class Router:
            def _update_kwargs_with_deployment(self, deployment, kwargs, function_name=None):
                kwargs["model_info"] = deployment["model_info"].copy()

            async def make_call(self, original_function, *args, **kwargs):
                deployment = {
                    "model_info": {"id": "browser-blocked-image", "order": 1},
                }
                self._update_kwargs_with_deployment(deployment, kwargs)
                return await original_function(**kwargs)

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_selected_deployment_marker_patch()

        error = RuntimeError(
            "Cloudflare Error 1010: Access denied. The site owner has blocked access based on your browser's signature."
        )
        error.status_code = 403

        async def original_function(**kwargs):
            raise error

        with self.assertRaises(RuntimeError):
            await Router().make_call(original_function)

        self.assertIsNone(getattr(error, "failed_deployment_id", None))
        self.assertFalse(hasattr(error, "excluded_deployment_ids"))

    async def test_generic_helper_retries_cloudflare_browser_signature_with_headers(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        attempts = []

        class Router:
            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                kwargs["model"] = model
                return await original_generic_function(**kwargs)

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        async def original_generic_function(**kwargs):
            attempts.append(kwargs.copy())
            if len(attempts) == 1:
                error = RuntimeError(
                    "Cloudflare Error 1010: Access denied. The site owner has blocked access based on your browser's signature."
                )
                error.status_code = 403
                raise error
            return {"ok": True}

        response = await Router()._ageneric_api_call_with_fallbacks_helper(
            "gpt-image-2",
            original_generic_function,
            call_type="aimage_generation",
            extra_headers={"User-Agent": "codex-local/1.2.3"},
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["extra_headers"], {"User-Agent": "codex-local/1.2.3"})
        self.assertIn("Mozilla/5.0", attempts[1]["extra_headers"]["User-Agent"])
        self.assertEqual(
            attempts[1]["extra_headers"]["Accept"],
            "application/json, text/plain, */*",
        )
        self.assertTrue(
            attempts[1]["litellm_metadata"][
                hooks._BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY
            ]
        )

    def test_browser_compatible_headers_replace_non_browser_user_agent(self) -> None:
        hooks, _ = load_hook_module()
        updated = hooks._with_browser_compatible_headers(
            {
                "litellm_metadata": {"api_base": "https://headers.example/v1"},
                "extra_headers": {"User-Agent": "Python-urllib/3.9"},
            }
        )

        self.assertIsNotNone(updated)
        self.assertIn("Mozilla/5.0", updated["extra_headers"]["User-Agent"])
        self.assertEqual(
            updated["extra_headers"]["Accept"], "application/json, text/plain, */*"
        )

    def test_selected_deployment_marker_overrides_target_order_for_failure_marking(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "model": "default-chat",
            "_target_order": 2,
            "model_info": {
                "id": "backup_provider-x-plus",
                "order": 2,
                "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
            },
        }
        selected_deployment = {
            "litellm_params": {"order": 3, "model": "openai/default-chat"},
            "model_info": {
                "id": "compat_provider-x-pro",
                "route_key": "compat_provider / openai/default-chat / key=x-pro / order=3",
            },
        }

        hooks._remember_selected_deployment_for_request(
            request_kwargs,
            selected_deployment,
        )
        error = RuntimeError("upstream 502")
        error.status_code = 502
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        self.assertEqual(error.failed_deployment_id, "compat_provider-x-pro")
        self.assertEqual(error.failed_deployment_order, 3)
        self.assertEqual(
            error.failed_deployment_route_key,
            "compat_provider / openai/default-chat / key=x-pro / order=3",
        )

    async def test_same_order_peer_fallback_entry_excludes_failed_deployment(self) -> None:
        hooks, _ = load_hook_module()

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                self.seen_model_name = model_name
                self.seen_team_id = team_id
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "order1-a"},
                    },
                    {
                        "litellm_params": {},
                        "model_info": {"id": "order1-b"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "order2-c"},
                    },
                ]

        router = Router()
        error = RuntimeError("upstream 503")
        error.status_code = 503
        error.failed_deployment_id = "order1-a"
        error.failed_deployment_order = 1

        entry = hooks._ordered_deployment_fallback_entry(
            router,
            error,
            {
                "model": "runtime-model-alias",
                "metadata": {"user_api_key_team_id": "team-a"},
            },
        )

        self.assertEqual(
            entry,
            {
                "model": "runtime-model-alias",
                "_target_order": 1,
                "_excluded_deployment_ids": ["order1-a"],
            },
        )
        self.assertEqual(router.seen_model_name, "runtime-model-alias")

    async def test_rate_limit_does_not_sweep_same_order_peer_fallback(self) -> None:
        hooks, _ = load_hook_module()

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "order1-a"},
                    },
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "order1-b"},
                    },
                ]

        error = RuntimeError("rate limit exceeded; retry after 10 seconds")
        error.status_code = 429
        error.failed_deployment_id = "order1-a"
        error.failed_deployment_order = 1

        entry = hooks._ordered_deployment_fallback_entry(
            Router(),
            error,
            {"model": "runtime-model-alias"},
        )

        self.assertIsNone(entry)

    async def test_routing_constraint_patch_filters_excluded_deployments_for_sync_route(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {"model_info": {"id": "order1-a"}},
                    {"model_info": {"id": "order1-b"}},
                    {"model_info": {"id": "order2-c"}},
                ]

            def get_available_deployment(self, model, request_kwargs=None):
                return self._get_all_deployments(model)

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_routing_constraint_patch()

        deployments = Router().get_available_deployment(
            "runtime-model-alias",
            request_kwargs={"_excluded_deployment_ids": ["order1-a"]},
        )

        self.assertEqual(
            [deployment["model_info"]["id"] for deployment in deployments],
            ["order1-b", "order2-c"],
        )

    async def test_order_peer_failover_patch_runs_same_order_before_larger_order(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        router_utils_module = types.ModuleType("litellm.router_utils")
        fallback_handlers_module = types.ModuleType(
            "litellm.router_utils.fallback_event_handlers"
        )
        seen = {"fallbacks": [], "original_called": False, "kwargs": None}

        async def run_async_fallback(*args, **kwargs):
            seen["fallbacks"].append(kwargs["fallback_model_group"])
            return "peer-response"

        fallback_handlers_module.run_async_fallback = run_async_fallback

        class Router:
            max_fallbacks = 8

            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "order1-a"},
                    },
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "order1-b"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "order2-c"},
                    },
                ]

            async def async_function_with_fallbacks_common_utils(
                self,
                e,
                disable_fallbacks,
                fallbacks,
                context_window_fallbacks,
                content_policy_fallbacks,
                model_group,
                args,
                kwargs,
            ):
                seen["original_called"] = True
                seen["kwargs"] = kwargs.copy()
                return "larger-order-response"

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        sys.modules["litellm.router_utils"] = router_utils_module
        sys.modules["litellm.router_utils.fallback_event_handlers"] = fallback_handlers_module
        hooks._install_order_peer_failover_patch()

        error = RuntimeError("upstream 503")
        error.status_code = 503
        error.failed_deployment_id = "order1-a"
        error.failed_deployment_order = 1

        response = await Router().async_function_with_fallbacks_common_utils(
            error,
            False,
            None,
            None,
            None,
            "runtime-model-alias",
            (),
            {"model": "runtime-model-alias", "original_function": object()},
        )

        self.assertEqual(response, "peer-response")
        self.assertFalse(seen["original_called"])
        self.assertEqual(
            seen["fallbacks"],
            [
                [
                    {
                        "model": "runtime-model-alias",
                        "_target_order": 1,
                        "_excluded_deployment_ids": ["order1-a"],
                    }
                ]
            ],
        )

    async def test_order_peer_failover_patch_marks_plain_selected_deployment_error(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        router_utils_module = types.ModuleType("litellm.router_utils")
        fallback_handlers_module = types.ModuleType(
            "litellm.router_utils.fallback_event_handlers"
        )
        seen = {"fallbacks": [], "original_called": False}

        async def run_async_fallback(*args, **kwargs):
            seen["fallbacks"].append(kwargs["fallback_model_group"])
            return "peer-response"

        fallback_handlers_module.run_async_fallback = run_async_fallback

        class Router:
            max_fallbacks = 8

            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {
                            "id": "backup_provider-x-plus",
                            "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
                        },
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {
                            "id": "compat_provider-r-plus",
                            "route_key": "compat_provider / openai/default-chat / key=r-plus / order=2",
                        },
                    },
                    {
                        "litellm_params": {"order": 3},
                        "model_info": {
                            "id": "backup_provider-x-pro",
                            "route_key": "backup_provider / openai/default-chat / key=x-pro / order=3",
                        },
                    },
                ]

            async def async_function_with_fallbacks_common_utils(
                self,
                e,
                disable_fallbacks,
                fallbacks,
                context_window_fallbacks,
                content_policy_fallbacks,
                model_group,
                args,
                kwargs,
            ):
                seen["original_called"] = True
                return "larger-order-response"

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        sys.modules["litellm.router_utils"] = router_utils_module
        sys.modules["litellm.router_utils.fallback_event_handlers"] = fallback_handlers_module
        hooks._install_order_peer_failover_patch()

        error = RuntimeError("APIError: upstream 500")
        error.status_code = 500

        response = await Router().async_function_with_fallbacks_common_utils(
            error,
            False,
            None,
            None,
            None,
            "default-chat",
            (),
            {
                "model": "default-chat",
                "call_type": "aresponses",
                "original_function": object(),
                "model_info": {
                    "id": "backup_provider-x-plus",
                    "order": 2,
                    "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
                },
            },
        )

        self.assertEqual(response, "peer-response")
        self.assertFalse(seen["original_called"])
        self.assertEqual(error.failed_deployment_id, "backup_provider-x-plus")
        self.assertEqual(error.failed_deployment_order, 2)
        self.assertEqual(
            seen["fallbacks"],
            [
                [
                    {
                        "model": "default-chat",
                        "_target_order": 2,
                        "_excluded_deployment_ids": ["backup_provider-x-plus"],
                    }
                ]
            ],
        )

    async def test_order_peer_failover_patch_wraps_to_lower_order_after_highest_order_failure(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        router_utils_module = types.ModuleType("litellm.router_utils")
        fallback_handlers_module = types.ModuleType(
            "litellm.router_utils.fallback_event_handlers"
        )
        seen = {"fallbacks": [], "original_called": False}

        async def run_async_fallback(*args, **kwargs):
            seen["fallbacks"].append(kwargs["fallback_model_group"])
            return "wrapped-response"

        fallback_handlers_module.run_async_fallback = run_async_fallback

        class Router:
            max_fallbacks = 8

            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "order1-a"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "order2-a"},
                    },
                ]

            async def async_function_with_fallbacks_common_utils(
                self,
                e,
                disable_fallbacks,
                fallbacks,
                context_window_fallbacks,
                content_policy_fallbacks,
                model_group,
                args,
                kwargs,
            ):
                seen["original_called"] = True
                return "unexpected-original"

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        sys.modules["litellm.router_utils"] = router_utils_module
        sys.modules["litellm.router_utils.fallback_event_handlers"] = fallback_handlers_module
        hooks._install_order_peer_failover_patch()

        error = RuntimeError("ServiceUnavailableError: upstream 500")
        error.status_code = 500
        error.failed_deployment_id = "order2-a"
        error.failed_deployment_order = 2

        response = await Router().async_function_with_fallbacks_common_utils(
            error,
            False,
            None,
            None,
            None,
            "runtime-model-alias",
            (),
            {"model": "runtime-model-alias", "original_function": object()},
        )

        self.assertEqual(response, "wrapped-response")
        self.assertFalse(seen["original_called"])
        self.assertEqual(
            seen["fallbacks"],
            [
                [
                    {
                        "model": "runtime-model-alias",
                        "_target_order": 1,
                        "_excluded_deployment_ids": ["order2-a"],
                    }
                ]
            ],
        )

    async def test_ordered_deployment_fallback_entry_wraps_to_lowest_order(self) -> None:
        hooks, _ = load_hook_module()

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "order1-a"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "order2-a"},
                    },
                    {
                        "litellm_params": {"order": 3},
                        "model_info": {"id": "order3-a"},
                    },
                ]

        error = RuntimeError("ServiceUnavailableError: upstream 503")
        error.status_code = 503
        error.failed_deployment_id = "order3-a"
        error.failed_deployment_order = 3

        entry = hooks._ordered_deployment_fallback_entry(
            Router(),
            error,
            {"model": "runtime-model-alias"},
        )

        self.assertEqual(
            entry,
            {
                "model": "runtime-model-alias",
                "_target_order": 1,
                "_excluded_deployment_ids": ["order3-a"],
            },
        )

    async def test_image_generation_reuses_common_same_order_peer_fallback(self) -> None:
        hooks, _ = load_hook_module()

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "backup_provider-x-pro"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "backup_provider-x-image"},
                    },
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "compat_provider-r-pro"},
                    },
                ]

        error = RuntimeError("ServiceUnavailableError: upstream 503")
        error.status_code = 503
        error.failed_deployment_id = "backup_provider-x-pro"
        error.failed_deployment_order = 1
        entry = hooks._ordered_deployment_fallback_entry(
            Router(),
            error,
            {
                "model": "gpt-image-2",
                "call_type": "aimage_generation",
                "metadata": {"model_group": "gpt-image-2"},
            },
        )

        self.assertEqual(
            entry,
            {
                "model": "gpt-image-2",
                "_target_order": 1,
                "_excluded_deployment_ids": ["backup_provider-x-pro"],
            },
        )

    async def test_image_generation_reuses_common_next_order_fallback(self) -> None:
        hooks, _ = load_hook_module()

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "backup_provider-x-pro"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "backup_provider-x-image"},
                    },
                ]

        error = RuntimeError("ServiceUnavailableError: upstream 503")
        error.status_code = 503
        error.failed_deployment_id = "backup_provider-x-pro"
        error.failed_deployment_order = 1
        entry = hooks._ordered_deployment_fallback_entry(
            Router(),
            error,
            {
                "model": "gpt-image-2",
                "call_type": "aimage_generation",
                "metadata": {"model_group": "gpt-image-2"},
            },
        )

        self.assertEqual(
            entry,
            {
                "model": "gpt-image-2",
                "_target_order": 2,
                "_excluded_deployment_ids": ["backup_provider-x-pro"],
            },
        )

    async def test_image_generation_order_two_failure_continues_to_higher_order_before_wrap(self) -> None:
        hooks, _ = load_hook_module()

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "backup_provider-x-pro"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "backup_provider-x-image"},
                    },
                    {
                        "litellm_params": {"order": 3},
                        "model_info": {"id": "last-resort"},
                    },
                ]

        error = RuntimeError("ServiceUnavailableError: upstream 503")
        error.status_code = 503
        error.failed_deployment_id = "backup_provider-x-image"
        error.failed_deployment_order = 2
        entry = hooks._ordered_deployment_fallback_entry(
            Router(),
            error,
            {
                "model": "gpt-image-2",
                "call_type": "aimage_generation",
                "metadata": {"model_group": "gpt-image-2"},
            },
        )

        self.assertEqual(
            entry,
            {
                "model": "gpt-image-2",
                "_target_order": 3,
                "_excluded_deployment_ids": ["backup_provider-x-image"],
            },
        )

    async def test_image_generation_fallback_exhaustion_sanitizes_balance_error(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        router_utils_module = types.ModuleType("litellm.router_utils")
        fallback_handlers_module = types.ModuleType(
            "litellm.router_utils.fallback_event_handlers"
        )
        seen = {"fallback_called": False, "original_common_called": False}

        async def run_async_fallback(*args, **kwargs):
            seen["fallback_called"] = True
            return "unexpected-fallback"

        fallback_handlers_module.run_async_fallback = run_async_fallback

        class Router:
            max_fallbacks = 8

            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "primary-x-image"},
                    },
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "peer-x-image"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "last-x-image"},
                    },
                ]

            async def async_function_with_fallbacks_common_utils(
                self,
                e,
                disable_fallbacks,
                fallbacks,
                context_window_fallbacks,
                content_policy_fallbacks,
                model_group,
                args,
                kwargs,
            ):
                seen["original_common_called"] = True
                return "unexpected-original-common"

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        sys.modules["litellm.router_utils"] = router_utils_module
        sys.modules["litellm.router_utils.fallback_event_handlers"] = fallback_handlers_module
        hooks._install_order_peer_failover_patch()

        error = RuntimeError("insufficient account balance")
        error.status_code = 403
        error.failed_deployment_id = "last-x-image"
        error.failed_deployment_order = 2
        error.excluded_deployment_ids = ["primary-x-image", "peer-x-image"]

        with self.assertRaises(Exception) as context:
            await Router().async_function_with_fallbacks_common_utils(
                error,
                False,
                None,
                None,
                None,
                "gpt-image-2",
                (),
                {
                    "model": "gpt-image-2",
                    "call_type": "aimage_generation",
                    "metadata": {"model_group": "gpt-image-2"},
                    "_target_order": 2,
                    "_excluded_deployment_ids": [
                        "primary-x-image",
                        "peer-x-image",
                        "last-x-image",
                    ],
                },
            )

        sanitized = context.exception
        self.assertIs(sanitized.__cause__, error)
        self.assertEqual(getattr(sanitized, "status_code", None), 503)
        self.assertIn("after LiteLLM fallback retries", str(sanitized))
        self.assertIn("upstream auth or balance error", str(sanitized))
        self.assertFalse(seen["fallback_called"])
        self.assertFalse(seen["original_common_called"])

    async def test_image_generation_policy_error_uses_common_terminal_suppression(self) -> None:
        hooks, _ = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        router_utils_module = types.ModuleType("litellm.router_utils")
        fallback_handlers_module = types.ModuleType(
            "litellm.router_utils.fallback_event_handlers"
        )
        seen = {"fallback_called": False, "original_common_called": False}

        async def run_async_fallback(*args, **kwargs):
            seen["fallback_called"] = True
            return "unexpected-fallback"

        fallback_handlers_module.run_async_fallback = run_async_fallback

        class Router:
            max_fallbacks = 8

            async def async_function_with_fallbacks_common_utils(
                self,
                e,
                disable_fallbacks,
                fallbacks,
                context_window_fallbacks,
                content_policy_fallbacks,
                model_group,
                args,
                kwargs,
            ):
                seen["original_common_called"] = True
                return "unexpected-original-common"

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        sys.modules["litellm.router_utils"] = router_utils_module
        sys.modules["litellm.router_utils.fallback_event_handlers"] = fallback_handlers_module
        hooks._install_order_peer_failover_patch()

        error = RuntimeError("invalid_request_error: prompt violates content policy")
        error.status_code = 400

        with self.assertRaises(RuntimeError) as context:
            await Router().async_function_with_fallbacks_common_utils(
                error,
                False,
                None,
                None,
                None,
                "gpt-image-2",
                (),
                {
                    "model": "gpt-image-2",
                    "call_type": "aimage_generation",
                    "metadata": {"model_group": "gpt-image-2"},
                },
            )

        self.assertIs(context.exception, error)
        self.assertFalse(seen["fallback_called"])
        self.assertFalse(seen["original_common_called"])
        self.assertIsNone(getattr(context.exception, "failed_deployment_id", None))
        self.assertIsNone(getattr(context.exception, "failed_deployment_order", None))
        self.assertFalse(hooks._is_priority_deployment_failover_error(context.exception))
        self.assertFalse(hooks._should_sanitize_final_upstream_route_error(context.exception))
        self.assertIsNone(
            hooks._ordered_deployment_fallback_entry(
                Router(),
                context.exception,
                {
                    "model": "gpt-image-2",
                    "call_type": "aimage_generation",
                    "metadata": {"model_group": "gpt-image-2"},
                },
            )
        )


if __name__ == "__main__":
    unittest.main()
