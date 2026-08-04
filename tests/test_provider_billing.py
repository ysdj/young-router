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
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler


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

    def one_model_config(self, api_base: str = "https://billing.example.test/v1") -> Path:
        return self.write_config(
            f"""
            providers:
              generic:
                api_base: {api_base}
                api_keys: [{{name: default, value: replace-me}}]
            model_list:
              - model_name: chat
                litellm_params: {{model: openai/chat, api_base: {api_base}}}
                model_info: {{id: a1b2c3d4, provider: generic, api_key_name: default, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}}
            """
        )

    def two_model_config(self) -> Path:
        return self.write_config(
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

    def two_models_same_credential_config(self) -> Path:
        return self.write_config(
            """
            providers:
              generic:
                api_base: https://billing.example.test/v1
                api_keys: [{name: default, value: replace-me}]
            model_list:
              - model_name: one
                litellm_params: {model: openai/one, api_base: https://billing.example.test/v1}
                model_info: {id: a1b2c3d4, provider: generic, api_key_name: default, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
              - model_name: two
                litellm_params: {model: openai/two, api_base: https://billing.example.test/v1}
                model_info: {id: a1b2c3d5, provider: generic, api_key_name: default, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
            """
        )

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
                side_effect=AssertionError("multiplier lookup must not discover proxies"),
            ),
        ):
            opener = provider_billing._billing_http_opener()

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
                api_keys: [{name: default, value: replace-me}]
              disabled-provider:
                enabled: false
                api_base: https://disabled.example.test/v1
                api_keys: [{name: default, value: replace-me-disabled}]
            model_list:
              - model_name: active-chat
                litellm_params: {model: openai/active-chat, api_base: https://billing.example.test/v1}
                model_info: {id: a1b2c3d4, provider: primary, api_key_name: default, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
              - model_name: disabled-chat
                litellm_params: {model: openai/disabled-chat, api_base: https://billing.example.test/v1}
                model_info: {id: a1b2c3d5, provider: primary, api_key_name: default, x-litellm-menu-model-enabled: false, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
              - model_name: hidden-chat
                litellm_params: {model: openai/hidden-chat, api_base: https://disabled.example.test/v1}
                model_info: {id: a1b2c3d6, provider: disabled-provider, api_key_name: default, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}
            """
        )

        targets = provider_billing.active_billing_targets(path)

        self.assertEqual(
            ["active-chat", "disabled-chat", "hidden-chat"],
            [target.model for target in targets],
        )
        self.assertEqual("replace-me", targets[0].api_key)
        self.assertEqual("replace-me-disabled", targets[2].api_key)

    def test_multiplier_refresh_never_requests_usage_or_exposes_balance(self) -> None:
        path = self.one_model_config()
        requested_paths: list[str] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            requested_paths.append(urlsplit(request.full_url).path)  # type: ignore[attr-defined]
            self.assertEqual("Bearer replace-me", request.get_header("Authorization"))  # type: ignore[attr-defined]
            return FakeResponse(
                {
                    "object": "sub2api.key_billing",
                    "billing_scope": "token",
                    "effective_rate_multiplier": 0.25,
                    "unused_quota": 999,
                }
            )

        payload = provider_billing.collect_billing(path, opener=opener)
        model = payload["providers"][0]["models"][0]
        rendered = json.dumps(payload)

        self.assertEqual(["/v1/sub2api/billing"], requested_paths)
        self.assertEqual("ok", model["status"])
        self.assertNotIn("balance", model)
        self.assertEqual(0.25, model["multiplier"]["value"])
        self.assertNotIn("usage", rendered.lower())
        self.assertNotIn("replace-me", rendered)
        self.assertNotIn("billing.example.test", rendered)

    def test_deployment_prefix_is_preserved_for_multiplier_endpoint(self) -> None:
        path = self.one_model_config("https://billing.example.test/api/v1")
        requested_paths: list[str] = []

        def opener(request: object, timeout: float) -> FakeResponse:
            requested_paths.append(urlsplit(request.full_url).path)  # type: ignore[attr-defined]
            return FakeResponse(
                {
                    "object": "sub2api.key_billing",
                    "billing_scope": "token",
                    "effective_rate_multiplier": "0.25",
                }
            )

        payload = provider_billing.collect_billing(path, opener=opener)

        self.assertEqual(["/api/v1/sub2api/billing"], requested_paths)
        self.assertEqual(0.25, payload["providers"][0]["models"][0]["multiplier"]["value"])

    def test_multiplier_shape_is_only_accepted_from_multiplier_endpoint(self) -> None:
        path = self.one_model_config()

        def opener(request: object, timeout: float) -> FakeResponse:
            return FakeResponse({"data": {"effective_rate_multiplier": 0.25}})

        payload = provider_billing.collect_billing(path, opener=opener)
        model = payload["providers"][0]["models"][0]

        self.assertEqual("unsupported", model["status"])
        self.assertNotIn("balance", model)
        self.assertEqual("unavailable", model["multiplier"]["status"])

    def test_auth_errors_are_never_represented_as_zero_balance(self) -> None:
        path = self.one_model_config()

        def opener(request: object, timeout: float) -> FakeResponse:
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO())  # type: ignore[attr-defined]

        payload = provider_billing.collect_billing(path, opener=opener)
        model = payload["providers"][0]["models"][0]

        self.assertEqual("auth_error", model["status"])
        self.assertNotIn("balance", model)
        self.assertEqual(401, model["http_status"])

    def test_unsupported_endpoint_does_not_leak_provider_details(self) -> None:
        path = self.one_model_config("https://private-looking.example.test/v1")

        def opener(request: object, timeout: float) -> FakeResponse:
            raise HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO())  # type: ignore[attr-defined]

        payload = provider_billing.collect_billing(path, opener=opener)
        rendered = json.dumps(payload)

        self.assertEqual("unsupported", payload["providers"][0]["models"][0]["status"])
        self.assertNotIn("replace-me", rendered)
        self.assertNotIn("private-looking.example.test", rendered)

    def test_a_bounded_refresh_skips_remaining_credentials_without_network_calls(self) -> None:
        path = self.two_model_config()
        original_limit = provider_billing.MAX_CREDENTIALS_PER_REFRESH
        provider_billing.MAX_CREDENTIALS_PER_REFRESH = 1
        self.addCleanup(setattr, provider_billing, "MAX_CREDENTIALS_PER_REFRESH", original_limit)
        calls = 0

        def opener(request: object, timeout: float) -> FakeResponse:
            nonlocal calls
            calls += 1
            return FakeResponse(
                {
                    "object": "sub2api.key_billing",
                    "billing_scope": "token",
                    "effective_rate_multiplier": 0.25,
                }
            )

        payload = provider_billing.collect_billing(path, opener=opener)
        statuses = [model["status"] for model in payload["providers"][0]["models"]]

        self.assertEqual(1, calls)
        self.assertEqual(["ok", "timeout"], statuses)

    def test_refresh_queries_a_shared_credential_once_for_multiple_models(self) -> None:
        path = self.two_models_same_credential_config()
        calls = 0

        def probe(target: provider_billing.BillingTarget, **_: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            self.assertEqual("replace-me", target.api_key)
            return provider_billing._multiplier_result(
                "ok",
                "Live effective key multiplier is available.",
                source="sub2api-key-billing",
                multiplier={"status": "ok", "value": 0.25},
            )

        with patch.object(provider_billing, "probe_model", side_effect=probe):
            payload = provider_billing.collect_billing(path)

        models = payload["providers"][0]["models"]
        self.assertEqual(1, calls)
        self.assertEqual(["ok", "ok"], [model["status"] for model in models])
        self.assertEqual([0.25, 0.25], [model["multiplier"]["value"] for model in models])

    def test_refresh_probes_independent_models_concurrently(self) -> None:
        path = self.two_model_config()
        arrived = threading.Event()
        calls: list[str] = []
        lock = threading.Lock()

        def probe(target: provider_billing.BillingTarget, **_: object) -> dict[str, object]:
            with lock:
                calls.append(target.api_key)
                if len(calls) == 2:
                    arrived.set()
            arrived.wait(0.1)
            return provider_billing._multiplier_result(
                "ok",
                "Live effective key multiplier is available.",
                source="sub2api-key-billing",
                multiplier={"status": "ok", "value": 0.25},
            )

        with patch.object(provider_billing, "probe_model", side_effect=probe):
            payload = provider_billing.collect_billing(path, timeout=0.05)

        models = payload["providers"][0]["models"]
        self.assertTrue(arrived.is_set())
        self.assertEqual(2, len(calls))
        self.assertEqual(["ok", "ok"], [model["status"] for model in models])

    def test_refresh_returns_at_global_deadline_without_waiting_for_slow_workers(self) -> None:
        path = self.two_model_config()

        def slow_probe(*_: object, **__: object) -> dict[str, object]:
            time.sleep(1.2)
            return provider_billing._multiplier_result("ok", "late")

        started = time.monotonic()
        with patch.object(provider_billing, "probe_model", side_effect=slow_probe):
            payload = provider_billing.collect_billing(path, timeout=0.5)
        elapsed = time.monotonic() - started

        models = payload["providers"][0]["models"]
        self.assertLess(elapsed, 0.9)
        self.assertEqual(["timeout", "timeout"], [model["status"] for model in models])


if __name__ == "__main__":
    unittest.main()
