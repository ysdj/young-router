from __future__ import annotations

import sqlite3
from unittest import mock

from hook_test_utils import *


class ProxyRoutingEdgesTests(HookTestCase):
    def test_sanitized_failure_message_is_built_once(self) -> None:
        hooks, _ = load_hook_module()
        calls = []
        original = hooks._sanitized_upstream_route_failure_message

        def counting(*args):
            calls.append(args)
            return original(*args)

        hooks._sanitized_upstream_route_failure_message = counting
        self.addCleanup(
            setattr, hooks, "_sanitized_upstream_route_failure_message", original
        )
        with self.assertRaises(Exception):
            hooks._raise_sanitized_upstream_route_failure(
                "edge-model", RuntimeError("temporary upstream issue"), {}
            )
        self.assertEqual(1, len(calls))

    def test_relay_capacity_text_is_read_once_per_message(self) -> None:
        hooks, _ = load_hook_module()
        calls = []
        original = hooks._upstream_relay_capacity_message

        def counting(exception):
            calls.append(exception)
            return original(exception)

        hooks._upstream_relay_capacity_message = counting
        self.addCleanup(
            setattr, hooks, "_upstream_relay_capacity_message", original
        )
        message = hooks._sanitized_upstream_route_failure_message(
            "edge-model",
            RuntimeError("relay refused: no available channel for this model"),
            {},
        )
        self.assertIn("upstream relay channel unavailable", message)
        self.assertEqual(1, len(calls))

    def test_selected_deployment_marker_keeps_no_request_credentials(self) -> None:
        hooks, _ = load_hook_module()
        marker = hooks._selected_deployment_request_marker(
            {
                "model_info": {"id": "route-a"},
                "litellm_params": {
                    "api_key": "secret-value",
                    "api_base": "https://provider.example/v1",
                },
            }
        )
        self.assertNotIn("request_params", marker)
        self.assertNotIn("api_key", marker["litellm_params"])

    def test_local_protocol_fallback_ignores_a_stale_surface(self) -> None:
        hooks, _ = load_hook_module()
        cache_key = "route-a|openai/responses"
        hooks._PROTOCOL_FALLBACKS[cache_key] = {
            "fallback_surface": "anthropic",
            "client_surface": "openai/responses",
            "from_surface": "openai/responses",
            "expires_at": time.time() + 60,
        }
        self.addCleanup(hooks._PROTOCOL_FALLBACKS.pop, cache_key, None)
        original = hooks._protocol_fallback_update_shared
        hooks._protocol_fallback_update_shared = lambda callback, **_options: None
        self.addCleanup(
            setattr, hooks, "_protocol_fallback_update_shared", original
        )
        surface = hooks._protocol_fallback_cached_surface(
            {"call_type": "responses"},
            {"model_info": {"id": "route-a", "upstream_url_surface": "openai/chat"}},
        )
        self.assertEqual("", surface)

    def test_protocol_fallback_lookup_is_read_only(self) -> None:
        hooks, _ = load_hook_module()
        state_module = hooks._state_module
        original_update = state_module._locked_json_state_update
        original_path = hooks._deployment_cooldown_file_path
        calls = []

        def fake(path, callback, **kwargs):
            calls.append(kwargs)
            callback({})
            return None

        state_module._locked_json_state_update = fake
        hooks._deployment_cooldown_file_path = lambda: "/tmp/state.json"
        self.addCleanup(
            setattr, state_module, "_locked_json_state_update", original_update
        )
        self.addCleanup(
            setattr, hooks, "_deployment_cooldown_file_path", original_path
        )
        hooks._protocol_fallback_cached_surface(
            {"call_type": "responses"},
            {"model_info": {"id": "route-a", "upstream_url_surface": "openai/chat"}},
        )
        self.assertTrue(calls[0].get("read_only"))

    def test_shared_state_transaction_errors_fall_back_to_local_state(self) -> None:
        hooks, _ = load_hook_module()
        state_module = hooks._state_module
        original_update = state_module._locked_json_state_update
        original_path = hooks._deployment_cooldown_file_path

        def failing(_path, _callback, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        state_module._locked_json_state_update = failing
        hooks._deployment_cooldown_file_path = lambda: "/tmp/state.json"
        self.addCleanup(
            setattr, state_module, "_locked_json_state_update", original_update
        )
        self.addCleanup(
            setattr, hooks, "_deployment_cooldown_file_path", original_path
        )
        self.assertIsNone(
            hooks._deployment_cooldown_update_shared(
                lambda _cooldowns, _now: None
            )
        )

    def test_request_log_reads_completion_start_once(self) -> None:
        hooks, _ = load_hook_module()
        calls = []
        original = hooks._completion_start_time

        def counting(request_kwargs, end_time=None):
            calls.append(end_time)
            return original(request_kwargs, end_time)

        hooks._completion_start_time = counting
        self.addCleanup(setattr, hooks, "_completion_start_time", original)
        start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(seconds=2)
        hooks._request_log_record(
            "success",
            {"completion_start_time": start + timedelta(seconds=1)},
            start_time=start,
            end_time=end,
        )
        self.assertEqual(1, len(calls))

    def test_usage_summary_reads_text_token_counts(self) -> None:
        hooks, _ = load_hook_module()
        usage = hooks._usage_summary(
            {
                "usage": {
                    "prompt_tokens": "120",
                    "completion_tokens": "7",
                    "total_tokens": "127",
                }
            },
            {},
        )
        self.assertEqual(120, usage["prompt_tokens"])
        self.assertEqual(7, usage["completion_tokens"])
        self.assertEqual(127, usage["total_tokens"])

    def test_repeated_start_time_keeps_the_earliest_stamp_at_capacity(self) -> None:
        hooks, _ = load_hook_module()
        routing = sys.modules["young_router.proxy.routing"]
        now = time.monotonic()
        observed_at = datetime.now(timezone.utc)
        store = {
            "req": (now - 2.0, observed_at),
            "other": (now - 1.0, observed_at),
        }
        with mock.patch.object(routing, "_FIRST_STREAM_OUTPUT_TIMES_MAX", 2):
            routing._remember_request_time(store, "req", observed_at, replace=False)
        self.assertEqual(now - 2.0, store["req"][0])
        self.assertIn("other", store)

    def test_a_duplicate_success_claim_keeps_live_claims_at_capacity(self) -> None:
        hooks, _ = load_hook_module()
        routing = sys.modules["young_router.proxy.routing"]
        now = time.monotonic()
        claims = {"other": now - 2.0, "req": now - 1.0}
        with mock.patch.object(
            routing, "_TRACED_DEPLOYMENT_SUCCESSES", claims
        ), mock.patch.object(routing, "_FIRST_STREAM_OUTPUT_TIMES_MAX", 2):
            self.assertFalse(routing._claim_traced_deployment_success(" req "))
        self.assertIn("other", claims)
        self.assertEqual(now - 1.0, claims["req"])

    def test_forced_cooldown_recording_bypasses_the_once_per_attempt_gate(self) -> None:
        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        self.set_env(routing._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(routing._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        self.set_env(routing._DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED_ENV, "1")
        routing._DEPLOYMENT_COOLDOWNS.clear()
        self.addCleanup(routing._DEPLOYMENT_COOLDOWNS.clear)
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        setattr(error, routing._DEPLOYMENT_COOLDOWN_FAILURE_RECORDED_ATTR, True)
        request = {
            "litellm_params": {"model": "openai/route-a"},
            "model_info": {"id": "route-a"},
        }
        with mock.patch.object(routing, "_deployment_cooldown_update_shared", lambda _callback: None):
            routing._record_deployment_failure_for_cooldown(
                error, request, force=True
            )
        self.assertTrue(
            any(
                state.get("cooldown_until")
                for state in routing._DEPLOYMENT_COOLDOWNS.values()
            )
        )
