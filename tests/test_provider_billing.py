from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import provider_billing  # noqa: E402


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


class ProviderBillingTests(unittest.TestCase):
    def write_config(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.yaml"
        path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
        return path

    def test_billing_opener_ignores_inherited_proxies_by_default(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "http_proxy": "http://proxy.example.test:8080",
                    "https_proxy": "http://proxy.example.test:8080",
                },
                clear=True,
            ),
            patch(
                "urllib.request.getproxies",
                side_effect=AssertionError("billing lookup must not discover proxies"),
            ),
        ):
            opener = provider_billing._billing_http_opener()

        # An empty ProxyHandler has no protocol handlers of its own, so urllib
        # omits it from the final list. Its presence during build is what keeps
        # urllib from adding the environment-derived default ProxyHandler.
        self.assertFalse(
            any(isinstance(handler, ProxyHandler) for handler in opener.handlers)
        )

    def test_billing_opener_honors_proxies_only_with_explicit_opt_in(self) -> None:
        with patch.dict(
            os.environ,
            {
                "http_proxy": "http://proxy.example.test:8080",
                "https_proxy": "http://proxy.example.test:8080",
                "LITELLM_USE_SYSTEM_PROXIES": "1",
            },
            clear=True,
        ):
            opener = provider_billing._billing_http_opener()

        proxy_handlers = [
            handler for handler in opener.handlers if isinstance(handler, ProxyHandler)
        ]
        self.assertEqual(1, len(proxy_handlers))
        self.assertEqual(
            "http://proxy.example.test:8080", proxy_handlers[0].proxies["http"]
        )
        self.assertEqual(
            "http://proxy.example.test:8080", proxy_handlers[0].proxies["https"]
        )

    def test_active_targets_include_disabled_provider_and_models(self) -> None:
        path = self.write_config(
            """
            providers:
              primary:
                api_base: https://billing.example.test/v1
                api_keys:
                  - name: default
                    value: replace-me
              disabled-provider:
                enabled: false
                api_base: https://disabled.example.test/v1
                api_keys:
                  - name: default
                    value: replace-me-disabled
            model_list:
              - model_name: active-chat
                litellm_params:
                  model: openai/active-chat
                  api_base: https://billing.example.test/v1
                model_info:
                  id: a1b2c3d4
                  provider: primary
                  api_key_name: default
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
              - model_name: disabled-chat
                litellm_params:
                  model: openai/disabled-chat
                  api_base: https://billing.example.test/v1
                model_info:
                  id: a1b2c3d5
                  provider: primary
                  api_key_name: default
                  x-litellm-menu-model-enabled: false
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
              - model_name: hidden-chat
                litellm_params:
                  model: openai/hidden-chat
                  api_base: https://disabled.example.test/v1
                model_info:
                  id: a1b2c3d6
                  provider: disabled-provider
                  api_key_name: default
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )

        targets = provider_billing.active_billing_targets(path)

        self.assertEqual(
            ["active-chat", "disabled-chat", "hidden-chat"],
            [target.model for target in targets],
        )
        self.assertEqual("primary", targets[0].provider)
        self.assertEqual("replace-me", targets[0].api_key)
        self.assertEqual("primary", targets[1].provider)
        self.assertEqual("disabled-provider", targets[2].provider)
        self.assertEqual("replace-me-disabled", targets[2].api_key)

    def test_newapi_token_usage_is_recognized_by_response_shape_and_sanitized(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://unrelated-host.example.test/v1
                api_keys:
                  - name: default
                    value: replace-me
            model_list:
              - model_name: chat
                litellm_params:
                  model: openai/upstream-chat
                  api_base: https://unrelated-host.example.test/v1
                model_info:
                  id: a1b2c3d4
                  provider: generic
                  api_key_name: default
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )
        requested_paths: list[str] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            url = request.full_url  # type: ignore[attr-defined]
            requested_paths.append(urlsplit(url).path)
            if url.endswith("/v1/usage"):
                raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())
            self.assertEqual("Bearer replace-me", request.get_header("Authorization"))  # type: ignore[attr-defined]
            return FakeResponse(
                {
                    "data": {
                        "object": "token_usage",
                        "total_granted": 100,
                        "total_used": 37,
                        "total_available": 63,
                    }
                }
            )

        payload = provider_billing.collect_billing(path, opener=opener)
        rendered = json.dumps(payload)
        account = payload["providers"][0]["accounts"][0]
        model = payload["providers"][0]["models"][0]

        self.assertEqual(["/v1/usage", "/v1/sub2api/billing", "/api/usage/token/"], requested_paths)
        self.assertEqual("ok", account["status"])
        self.assertEqual("newapi-token-usage", account["source"])
        self.assertEqual(63, account["balance"]["value"])
        self.assertEqual({"used": 37, "limit": 100, "unit": "quota"}, account["usage"])
        self.assertEqual("unavailable", model["multiplier"]["status"])
        self.assertNotIn("replace-me", rendered)
        self.assertNotIn("unrelated-host.example.test", rendered)

    def test_newapi_usage_path_preserves_a_deployment_prefix_without_host_heuristics(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://another.example.test/api/v1
                api_keys:
                  - name: default
                    value: replace-me
            model_list:
              - model_name: chat
                litellm_params:
                  model: openai/upstream-chat
                  api_base: https://another.example.test/api/v1
                model_info:
                  id: a1b2c3d4
                  provider: generic
                  api_key_name: default
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )

        requested_paths: list[str] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            url = request.full_url  # type: ignore[attr-defined]
            requested_paths.append(urlsplit(url).path)
            if url.endswith("/api/usage/token/"):
                return FakeResponse(
                    {"data": {"object": "token_usage", "total_granted": 50, "total_used": 8, "total_available": 42}}
                )
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())

        payload = provider_billing.collect_billing(path, opener=opener)
        account = payload["providers"][0]["accounts"][0]

        self.assertEqual("ok", account["status"])
        self.assertEqual("newapi-token-usage", account["source"])
        self.assertEqual(42, account["balance"]["value"])
        self.assertIsNone(account["group"])
        self.assertEqual(
            ["/api/v1/usage", "/api/v1/sub2api/billing", "/api/usage/token/"],
            requested_paths,
        )

    def test_token_usage_shape_wins_over_an_unrelated_candidate_path(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://another.example.test/v1
                api_keys:
                  - name: default
                    value: replace-me
            model_list:
              - model_name: chat
                litellm_params:
                  model: openai/upstream-chat
                  api_base: https://another.example.test/v1
                model_info:
                  id: a1b2c3d4
                  provider: generic
                  api_key_name: default
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )

        def opener(request: object, timeout: float) -> FakeResponse:
            return FakeResponse(
                {"data": {"object": "token_usage", "total_granted": 4, "total_used": 1, "total_available": 3}}
            )

        payload = provider_billing.collect_billing(path, opener=opener)
        account = payload["providers"][0]["accounts"][0]

        self.assertEqual("newapi-token-usage", account["source"])
        self.assertEqual(3, account["balance"]["value"])

    def test_unlimited_token_usage_never_becomes_a_negative_balance(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://another.example.test/v1
                api_keys: [{name: default, value: replace-me}]
            model_list:
              - model_name: chat
                litellm_params: {model: openai/chat, api_base: https://another.example.test/v1}
                model_info: {id: a1b2c3d4, provider: generic, api_key_name: default, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
            """
        )

        def opener(request: object, timeout: float) -> FakeResponse:
            url = request.full_url  # type: ignore[attr-defined]
            if url.endswith("/api/usage/token/"):
                return FakeResponse(
                    {
                        "data": {
                            "object": "token_usage",
                            "unlimited_quota": True,
                            "total_used": 24042193,
                            "total_available": -24042193,
                        }
                    }
                )
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())

        payload = provider_billing.collect_billing(path, opener=opener, browser_probe=None)
        account = payload["providers"][0]["accounts"][0]

        self.assertEqual("ok", account["status"])
        self.assertIsNone(account["balance"])
        self.assertIn("unlimited quota", account["detail"])

    def test_browser_fallback_is_used_only_after_direct_probes_fail(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://newapi.example.test/v1
                api_keys: [{name: default, value: replace-me}]
            model_list:
              - model_name: chat
                litellm_params: {model: openai/chat, api_base: https://newapi.example.test/v1}
                model_info: {id: a1b2c3d4, provider: generic, api_key_name: default, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
            """
        )
        browser_calls: list[tuple[str, str]] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            url = request.full_url  # type: ignore[attr-defined]
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())

        def browser_probe(api_base: str, api_key: str, **_: object) -> tuple[str, int, dict[str, object]]:
            browser_calls.append((api_base, api_key))
            return (
                "ok",
                200,
                {
                    "success": True,
                    "data": {
                        "items": [
                            {
                                "key": api_key,
                                "remain_quota": 12.5,
                                "used_quota": 7.5,
                                "total_granted": 20,
                                "group": "cheap",
                                "__browser_multiplier": 0.13,
                                "__browser_match": True,
                            }
                        ]
                    },
                },
            )

        with patch.dict(os.environ, {"LITELLM_BROWSER_BILLING": "1"}):
            payload = provider_billing.collect_billing(
                path, opener=opener, browser_probe=browser_probe
            )
        account = payload["providers"][0]["accounts"][0]

        self.assertEqual([("https://newapi.example.test/v1", "replace-me")], browser_calls)
        self.assertEqual("newapi-browser-token-search", account["source"])
        self.assertEqual(12.5, account["balance"]["value"])
        self.assertEqual(0.13, account["multiplier"]["value"])
        self.assertNotIn("replace-me", json.dumps(payload))

    def test_browser_fallback_is_disabled_without_explicit_opt_in(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://newapi.example.test/v1
                api_keys: [{name: default, value: replace-me}]
            model_list:
              - model_name: chat
                litellm_params: {model: openai/chat, api_base: https://newapi.example.test/v1}
                model_info: {id: a1b2c3d4, provider: generic, api_key_name: default, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
            """
        )
        browser_calls: list[str] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            url = request.full_url  # type: ignore[attr-defined]
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())

        def browser_probe(api_base: str, api_key: str, **_: object) -> object:
            browser_calls.append(api_key)
            raise AssertionError("browser fallback must be opt-in")

        with patch.dict(os.environ, {"LITELLM_BROWSER_BILLING": ""}):
            payload = provider_billing.collect_billing(
                path, opener=opener, browser_probe=browser_probe
            )

        account = payload["providers"][0]["accounts"][0]
        self.assertEqual([], browser_calls)
        self.assertEqual("unsupported", account["status"])

    def test_sub2api_usage_and_effective_multiplier_are_shared_per_credential(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://not-a-special-host.example.test/v1
                api_keys:
                  - name: default
                    value: replace-me
            model_list:
              - model_name: first
                litellm_params:
                  model: openai/first
                  api_base: https://not-a-special-host.example.test/v1
                model_info:
                  id: a1b2c3d4
                  provider: generic
                  api_key_name: default
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
              - model_name: second
                litellm_params:
                  model: openai/second
                  api_base: https://not-a-special-host.example.test/v1
                model_info:
                  id: a1b2c3d5
                  provider: generic
                  api_key_name: default
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )
        calls = 0
        requested_paths: list[str] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            nonlocal calls
            calls += 1
            url = request.full_url  # type: ignore[attr-defined]
            requested_paths.append(urlsplit(url).path)
            if url.endswith("/v1/usage"):
                return FakeResponse(
                    {"mode": "quota_limited", "quota": {"limit": 20, "used": 7.5, "remaining": 12.5, "unit": "USD"}}
                )
            return FakeResponse(
                {"object": "sub2api.key_billing", "billing_scope": "token", "effective_rate_multiplier": 1.25}
            )

        payload = provider_billing.collect_billing(path, opener=opener)
        provider = payload["providers"][0]

        self.assertEqual(2, calls)
        self.assertEqual(["/v1/usage", "/v1/sub2api/billing"], requested_paths)
        self.assertEqual("ok", provider["status"])
        self.assertEqual("quota_limited", provider["accounts"][0]["mode"])
        self.assertEqual(12.5, provider["accounts"][0]["balance"]["value"])
        self.assertEqual(1.25, provider["accounts"][0]["multiplier"]["value"])
        self.assertEqual(["first", "second"], [model["name"] for model in provider["models"]])
        self.assertTrue(all(model["multiplier"]["status"] == "ok" for model in provider["models"]))

    def test_sub2api_multiplier_shape_is_only_accepted_from_its_billing_endpoint(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://billing.example.test/v1
                api_keys: [{name: default, value: replace-me}]
            model_list:
              - model_name: chat
                litellm_params: {model: openai/chat, api_base: https://billing.example.test/v1}
                model_info: {id: a1b2c3d4, provider: generic, api_key_name: default, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
            """
        )
        requested_paths: list[str] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            url = request.full_url  # type: ignore[attr-defined]
            requested_paths.append(urlsplit(url).path)
            if url.endswith("/v1/usage"):
                return FakeResponse(
                    {"object": "sub2api.key_billing", "effective_rate_multiplier": 1.25}
                )
            if url.endswith("/v1/sub2api/billing"):
                raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())
            return FakeResponse(
                {"data": {"object": "token_usage", "total_granted": 8, "total_used": 3, "total_available": 5}}
            )

        payload = provider_billing.collect_billing(path, opener=opener)
        account = payload["providers"][0]["accounts"][0]

        self.assertEqual("newapi-token-usage", account["source"])
        self.assertEqual("unavailable", account["multiplier"]["status"])
        self.assertEqual(["/v1/usage", "/v1/sub2api/billing", "/api/usage/token/"], requested_paths)

    def test_recognized_sub2api_balance_does_not_probe_newapi_after_multiplier_is_unavailable(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://billing.example.test/v1
                api_keys: [{name: default, value: replace-me}]
            model_list:
              - model_name: chat
                litellm_params: {model: openai/chat, api_base: https://billing.example.test/v1}
                model_info: {id: a1b2c3d4, provider: generic, api_key_name: default, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
            """
        )
        requested_paths: list[str] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            url = request.full_url  # type: ignore[attr-defined]
            requested_paths.append(urlsplit(url).path)
            if url.endswith("/v1/usage"):
                return FakeResponse({"remaining": 12, "unit": "USD"})
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())

        payload = provider_billing.collect_billing(path, opener=opener)
        account = payload["providers"][0]["accounts"][0]

        self.assertEqual("ok", account["status"])
        self.assertEqual("unavailable", account["multiplier"]["status"])
        self.assertEqual(["/v1/usage", "/v1/sub2api/billing"], requested_paths)

    def test_auth_errors_are_never_represented_as_zero_balance(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://billing.example.test/v1
                api_keys:
                  - name: default
                    value: replace-me
            model_list:
              - model_name: chat
                litellm_params:
                  model: openai/chat
                  api_base: https://billing.example.test/v1
                model_info:
                  id: a1b2c3d4
                  provider: generic
                  api_key_name: default
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )

        def opener(request: object, timeout: float) -> FakeResponse:
            url = request.full_url  # type: ignore[attr-defined]
            raise HTTPError(url, 401, "Unauthorized", {}, io.BytesIO())

        payload = provider_billing.collect_billing(path, opener=opener)
        account = payload["providers"][0]["accounts"][0]

        self.assertEqual("auth_error", account["status"])
        self.assertIsNone(account["balance"])
        self.assertEqual(401, account["http_status"])

    def test_user_account_jwt_requirement_does_not_imply_api_key_is_invalid(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://billing.example.test/v1
                api_keys:
                  - name: default
                    value: replace-me
            model_list:
              - model_name: chat
                litellm_params:
                  model: openai/chat
                  api_base: https://billing.example.test/v1
                model_info:
                  id: a1b2c3d4
                  provider: generic
                  api_key_name: default
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )

        def opener(request: object, timeout: float) -> FakeResponse:
            url = request.full_url  # type: ignore[attr-defined]
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())

        payload = provider_billing.collect_billing(path, opener=opener)
        account = payload["providers"][0]["accounts"][0]

        self.assertEqual("unsupported", account["status"])
        self.assertIsNone(account["balance"])
        self.assertNotIn("rejected", account["detail"])

    def test_invalid_or_unsupported_endpoints_do_not_leak_provider_details(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://private-looking.example.test/v1
                api_keys:
                  - name: default
                    value: replace-me
            model_list:
              - model_name: chat
                litellm_params:
                  model: openai/chat
                  api_base: https://private-looking.example.test/v1
                model_info:
                  id: a1b2c3d4
                  provider: generic
                  api_key_name: default
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )

        def opener(request: object, timeout: float) -> FakeResponse:
            url = request.full_url  # type: ignore[attr-defined]
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())

        payload = provider_billing.collect_billing(path, opener=opener)
        rendered = json.dumps(payload)

        self.assertEqual("unsupported", payload["providers"][0]["accounts"][0]["status"])
        self.assertNotIn("replace-me", rendered)
        self.assertNotIn("private-looking.example.test", rendered)

    def test_free_text_from_a_billing_endpoint_is_not_emitted(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://billing.example.test/v1
                api_keys: [{name: default, value: replace-me}]
            model_list:
              - model_name: chat
                litellm_params: {model: openai/chat, api_base: https://billing.example.test/v1}
                model_info: {id: a1b2c3d4, provider: generic, api_key_name: default, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
            """
        )

        def opener(request: object, timeout: float) -> FakeResponse:
            url = request.full_url  # type: ignore[attr-defined]
            if url.endswith("/v1/usage"):
                return FakeResponse({"remaining": 2, "unit": "USD", "mode": "token sk-secret.example.test"})
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())

        payload = provider_billing.collect_billing(path, opener=opener)
        rendered = json.dumps(payload)
        self.assertNotIn("sk-secret.example.test", rendered)

    def test_a_bounded_refresh_marks_remaining_accounts_without_network_calls(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://billing.example.test/v1
                api_keys:
                  - {name: one, value: replace-one}
                  - {name: two, value: replace-two}
            model_list:
              - model_name: one
                litellm_params: {model: openai/one, api_base: https://billing.example.test/v1, api_key: replace-one}
                model_info: {id: a1b2c3d4, provider: generic, api_key_name: one, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
              - model_name: two
                litellm_params: {model: openai/two, api_base: https://billing.example.test/v1, api_key: replace-two}
                model_info: {id: a1b2c3d5, provider: generic, api_key_name: two, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
            """
        )
        original_limit = provider_billing.MAX_ACCOUNTS_PER_REFRESH
        provider_billing.MAX_ACCOUNTS_PER_REFRESH = 1
        self.addCleanup(setattr, provider_billing, "MAX_ACCOUNTS_PER_REFRESH", original_limit)
        calls = 0

        def opener(request: object, timeout: float) -> FakeResponse:
            nonlocal calls
            calls += 1
            return FakeResponse({"remaining": 1, "unit": "USD"})

        payload = provider_billing.collect_billing(path, opener=opener)
        statuses = [account["status"] for account in payload["providers"][0]["accounts"]]
        self.assertEqual(2, calls)
        self.assertEqual(["ok", "timeout"], statuses)

    def test_refresh_probes_independent_accounts_concurrently(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://billing.example.test/v1
                api_keys:
                  - {name: one, value: replace-one}
                  - {name: two, value: replace-two}
            model_list:
              - model_name: one
                litellm_params: {model: openai/one, api_base: https://billing.example.test/v1, api_key: replace-one}
                model_info: {id: a1b2c3d4, provider: generic, api_key_name: one, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
              - model_name: two
                litellm_params: {model: openai/two, api_base: https://billing.example.test/v1, api_key: replace-two}
                model_info: {id: a1b2c3d5, provider: generic, api_key_name: two, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
            """
        )
        arrived = threading.Event()
        calls: list[str] = []
        lock = threading.Lock()

        def probe(api_base: str, api_key: str, **_: object) -> dict[str, object]:
            with lock:
                calls.append(api_key)
                if len(calls) == 2:
                    arrived.set()
            arrived.wait(0.1)
            return provider_billing._account_result(
                "ok",
                "Live provider billing data is available.",
                source="sub2api-v1-usage",
                balance={"kind": "balance", "value": 1, "unit": "USD"},
            )

        with patch.object(provider_billing, "probe_account", side_effect=probe):
            payload = provider_billing.collect_billing(path, timeout=0.05)

        accounts = payload["providers"][0]["accounts"]
        self.assertTrue(arrived.is_set())
        self.assertEqual(2, len(calls))
        self.assertEqual(["ok", "ok"], [account["status"] for account in accounts])

    def test_refresh_returns_at_global_deadline_without_waiting_for_slow_workers(self) -> None:
        path = self.write_config(
            """
            providers:
              generic:
                api_base: https://billing.example.test/v1
                api_keys:
                  - {name: one, value: replace-one}
                  - {name: two, value: replace-two}
            model_list:
              - model_name: one
                litellm_params: {model: openai/one, api_base: https://billing.example.test/v1, api_key: replace-one}
                model_info: {id: a1b2c3d4, provider: generic, api_key_name: one, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
              - model_name: two
                litellm_params: {model: openai/two, api_base: https://billing.example.test/v1, api_key: replace-two}
                model_info: {id: a1b2c3d5, provider: generic, api_key_name: two, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
            """
        )

        def slow_probe(*_: object, **__: object) -> dict[str, object]:
            time.sleep(1.2)
            return provider_billing._account_result("ok", "late")

        started = time.monotonic()
        with patch.object(provider_billing, "probe_account", side_effect=slow_probe):
            payload = provider_billing.collect_billing(path, timeout=0.5)
        elapsed = time.monotonic() - started

        accounts = payload["providers"][0]["accounts"]
        self.assertLess(elapsed, 0.9)
        self.assertEqual(["timeout", "timeout"], [account["status"] for account in accounts])


if __name__ == "__main__":
    unittest.main()
