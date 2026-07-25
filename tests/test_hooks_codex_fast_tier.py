from __future__ import annotations

import shutil

from hook_test_utils import *


class HookCodexFastTierTests(HookTestCase):
    def _set_codex_config(self, text: str) -> Path:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        self.set_env("CODEX_HOME", str(directory))
        config_path = directory / "config.toml"
        config_path.write_text(text, encoding="utf-8")
        return config_path

    @staticmethod
    def _codex_responses_request(*, service_tier: object = ... ) -> dict:
        body = {"model": "default-chat", "input": "hello"}
        if service_tier is not ...:
            body["service_tier"] = service_tier
        return {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "hello",
            "extra_headers": {"User-Agent": "Codex Desktop/1.0"},
            "proxy_server_request": {
                "url": "http://127.0.0.1:4000/v1/responses",
                "method": "POST",
                "headers": {
                    "X-Codex-Turn-Metadata": '{"request_kind":"turn"}',
                    "User-Agent": "Codex Desktop/1.0",
                },
                "body": body,
            },
        }

    async def test_codex_fast_config_injects_native_priority(self) -> None:
        hooks, _ = load_hook_module()
        self._set_codex_config(
            'service_tier = "fast"\n[features]\nfast_mode = true\n'
        )
        hook = hooks.LiteLLMMenuHook()

        updated = await hook.async_pre_call_deployment_hook(
            self._codex_responses_request(), "aresponses"
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["service_tier"], "priority")
        self.assertEqual(
            updated["litellm_metadata"]["codex_fast_default_service_tier"],
            "priority",
        )

    async def test_explicit_service_tier_is_never_overwritten(self) -> None:
        hooks, _ = load_hook_module()
        self._set_codex_config(
            'service_tier = "priority"\n[features]\nfast_mode = true\n'
        )
        hook = hooks.LiteLLMMenuHook()
        for explicit_tier in ("standard", "priority", "flex", None):
            original = self._codex_responses_request(service_tier=explicit_tier)

            updated = await hook.async_pre_call_deployment_hook(original, "aresponses")

            self.assertIsNone(updated)
            self.assertNotIn("service_tier", original)
            self.assertEqual(
                original["proxy_server_request"]["body"]["service_tier"],
                explicit_tier,
            )

    async def test_non_codex_responses_request_is_untouched(self) -> None:
        hooks, _ = load_hook_module()
        self._set_codex_config(
            'service_tier = "fast"\n[features]\nfast_mode = true\n'
        )
        hook = hooks.LiteLLMMenuHook()
        original = self._codex_responses_request()
        original["proxy_server_request"]["headers"] = {"User-Agent": "curl/8.0"}
        original["extra_headers"] = {"User-Agent": "curl/8.0"}

        updated = await hook.async_pre_call_deployment_hook(original, "aresponses")

        self.assertIsNone(updated)
        self.assertNotIn("service_tier", original)

    async def test_non_post_responses_request_is_untouched(self) -> None:
        hooks, _ = load_hook_module()
        self._set_codex_config(
            'service_tier = "fast"\n[features]\nfast_mode = true\n'
        )
        hook = hooks.LiteLLMMenuHook()
        original = self._codex_responses_request()
        original["proxy_server_request"]["method"] = "GET"

        updated = await hook.async_pre_call_deployment_hook(original, "aresponses")

        self.assertIsNone(updated)
        self.assertNotIn("service_tier", original)

    async def test_fast_mode_disabled_does_not_inject(self) -> None:
        hooks, _ = load_hook_module()
        self._set_codex_config(
            'service_tier = "fast"\n[features]\nfast_mode = false\n'
        )
        hook = hooks.LiteLLMMenuHook()
        original = self._codex_responses_request()

        updated = await hook.async_pre_call_deployment_hook(original, "aresponses")

        self.assertIsNone(updated)
        self.assertNotIn("service_tier", original)

    async def test_config_apply_style_atomic_replace_refreshes_next_request(self) -> None:
        hooks, _ = load_hook_module()
        config_path = self._set_codex_config(
            'service_tier = "fast"\n[features]\nfast_mode = false\n'
        )
        hook = hooks.LiteLLMMenuHook()
        self.assertIsNone(
            await hook.async_pre_call_deployment_hook(
                self._codex_responses_request(), "aresponses"
            )
        )

        replacement = config_path.with_suffix(".tmp")
        replacement.write_text(
            'service_tier = "priority"\n[features]\nfast_mode = true\n',
            encoding="utf-8",
        )
        replacement.replace(config_path)

        updated = await hook.async_pre_call_deployment_hook(
            self._codex_responses_request(), "aresponses"
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["service_tier"], "priority")

        replacement = config_path.with_suffix(".tmp")
        replacement.write_text(
            'service_tier = "fast"\n[features]\nfast_mode = false\n',
            encoding="utf-8",
        )
        replacement.replace(config_path)
        self.assertIsNone(
            await hook.async_pre_call_deployment_hook(
                self._codex_responses_request(), "aresponses"
            )
        )

    async def test_trace_records_injection_and_priority_rejection_without_secrets(self) -> None:
        hooks, _ = load_hook_module()
        self._set_codex_config(
            'service_tier = "fast"\n[features]\nfast_mode = true\n'
        )
        hook = hooks.LiteLLMMenuHook()
        calls = []
        original_trace = hooks._route_trace
        hooks._route_trace = lambda event, **fields: calls.append((event, fields))
        self.addCleanup(setattr, hooks, "_route_trace", original_trace)

        updated = await hook.async_pre_call_deployment_hook(
            self._codex_responses_request(), "aresponses"
        )
        assert updated is not None
        await hook.async_log_failure_event(
            updated,
            RuntimeError("upstream rejected priority SECRET_UPSTREAM_BODY"),
            None,
            None,
        )

        injected = [fields for event, fields in calls if event == "codex_fast_default_service_tier_injected"]
        result = [fields for event, fields in calls if event == "codex_fast_default_service_tier_result"]
        self.assertTrue(injected[0]["codex_fast_default_injected"])
        self.assertEqual(injected[0]["service_tier"], "priority")
        self.assertEqual(injected[0]["requested_service_tier"], "priority")
        self.assertEqual(result[0]["outcome"], "failure")
        self.assertTrue(result[0]["priority_rejected_or_unconfirmed"])
        self.assertNotIn("SECRET_UPSTREAM_BODY", json.dumps(calls))

    async def test_generic_patch_path_injects_before_its_upstream_call(self) -> None:
        hooks, _ = load_hook_module()
        self._set_codex_config(
            'service_tier = "fast"\n[features]\nfast_mode = true\n'
        )
        router_module = types.ModuleType("litellm.router")
        observed = []

        class Router:
            async def _ageneric_api_call_with_fallbacks_helper(
                self, model, original_generic_function, **kwargs
            ):
                observed.append(kwargs.copy())
                return await original_generic_function(**kwargs)

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        async def upstream(**kwargs):
            return {"service_tier": kwargs.get("service_tier")}

        request = self._codex_responses_request()
        request.pop("model")
        response = await Router()._ageneric_api_call_with_fallbacks_helper(
            "default-chat", upstream, **request
        )

        self.assertEqual(response["service_tier"], "priority")
        self.assertEqual(observed[0]["service_tier"], "priority")
