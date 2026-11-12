from __future__ import annotations

from hook_test_utils import *


class HookRoutingTests(HookTestCase):
    async def test_encrypted_history_continuation_keeps_eligible_routes(self) -> None:
        hooks, _proxy_server = load_hook_module()
        deployments = [
            {"model_info": {"id": "route-low", "order": 1}},
            {"model_info": {"id": "route-target", "order": 2}},
        ]
        replay = {
            "call_type": "aresponses",
            "input": [
                {
                    "type": "compaction",
                    "id": "cmp-existing",
                    "encrypted_content": "opaque-history",
                },
                {"type": "message", "role": "user", "content": "continue"},
            ],
            "client_metadata": {
                "session_id": "thread-encrypted-history",
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "_target_order": 2,
        }

        selected = await hooks.YoungRouterHook().async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs=replay,
        )

        self.assertEqual(
            [item["model_info"]["id"] for item in selected], ["route-target"]
        )

    def test_recovery_diagnostic_classifies_user_actionable_failures_without_error_text(self) -> None:
        hooks, _proxy_server = load_hook_module()

        cases = [
            (RuntimeError("insufficient credits for this account"), "billing"),
            (RuntimeError("API key unauthorized"), "authentication"),
            (RuntimeError("Cannot connect to host api.example.test"), "network"),
            (RuntimeError("rate limit exceeded"), "rate_limit"),
            (TimeoutError("upstream request timed out"), "timeout"),
            (RuntimeError("temporary upstream issue"), "unknown"),
        ]
        for error, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind):
                diagnostic = hooks._recovery_diagnostic(error)
                self.assertEqual(diagnostic["kind"], expected_kind)
                self.assertIn("title", diagnostic)
                self.assertIn("detail", diagnostic)
                self.assertNotIn("api.example.test", json.dumps(diagnostic))

        forbidden = RuntimeError("provider rejected secret-token-should-not-appear")
        diagnostic = hooks._recovery_diagnostic(forbidden)
        self.assertNotIn("secret-token-should-not-appear", json.dumps(diagnostic))

    def test_thinking_replay_refusal_keeps_looking_instead_of_ending_the_turn(self) -> None:
        """A route that refuses a replay without its thinking is not the last word.

        Observed on one DeepSeek thinking route: after the group's earlier routes
        answered 429/502, this one refused the replayed history with HTTP 400
        ``the `reasoning_text` in the thinking mode must be passed back to the
        api``.  Read as a deterministic request error it ended the turn as
        ``upstream_route_failure`` -- a retry tip the user cannot act on, for a
        history another route answers fine.  The refusal belongs to the attempt,
        never to the whole turn.
        """

        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_POLICY_REQUEST_ERROR_ENV, None)

        reasoning_text = RuntimeError(
            'litellm.BadRequestError: OpenAIException - {"error":{"message":'
            '"the `reasoning_text` in the thinking mode must be passed back to the api."}}'
        )
        reasoning_text.status_code = 400
        reasoning_content = RuntimeError(
            "litellm.BadRequestError: OpenAIException - "
            "The `reasoning_content` in the thinking mode must be passed back to the API."
        )
        reasoning_content.status_code = 400

        for refusal in (reasoning_text, reasoning_content):
            self.assertEqual(
                hooks._recovery_policy_for_exception(refusal),
                "recovery",
            )
            self.assertNotEqual(
                hooks._recovery_policy_for_exception(refusal),
                "error",
            )
            self.assertFalse(
                hooks._is_deployment_compatible_bad_request_error(refusal),
            )

        # An ordinary 400 is still the request's own verdict.
        invalid = RuntimeError("OpenAIException invalid_request_error: invalid input")
        invalid.status_code = 400
        self.assertEqual(hooks._recovery_policy_for_exception(invalid), "error")

    def test_recovery_policy_defaults_match_error_boundaries(self) -> None:
        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_POLICY_RATE_LIMIT_ENV, None)

        balance = RuntimeError("insufficient account balance")
        balance.status_code = 403
        rate_limit = RuntimeError("rate limit exceeded")
        rate_limit.status_code = 429
        overload = RuntimeError("upstream overloaded")
        overload.status_code = 503
        network = RuntimeError("Cannot connect to host api.example.test")
        network.status_code = 500
        start_timeout = TimeoutError("stream did not start")
        start_timeout.status_code = 504
        start_timeout.body = {"reason": "stream_start_timeout"}
        idle_timeout = TimeoutError("stream idle timeout")
        idle_timeout.status_code = 504
        idle_timeout.body = {"reason": "stream_idle_timeout"}
        request_error = RuntimeError("OpenAIException invalid_request_error: invalid input")
        request_error.status_code = 400
        account_pool_exhausted = RuntimeError("all available accounts exhausted")
        account_pool_exhausted.status_code = 403

        self.assertEqual(hooks._recovery_policy_for_exception(balance), "recovery_cooldown")
        # A rate limit is a route the next request must not try first again:
        # measured on the live gateway, one route refused 691 times and
        # recovered within five minutes four times, while every request kept
        # selecting it first.  The default parks it after the ordinary
        # failure threshold instead of retrying in place forever.
        self.assertEqual(hooks._recovery_policy_for_exception(rate_limit), "recovery_cooldown")
        # A 5xx is the route failing, whatever sentence it wears: an "overloaded"
        # 503 is a route the next request must not try first again.
        self.assertEqual(hooks._recovery_policy_for_exception(overload), "recovery_cooldown")
        self.assertEqual(hooks._recovery_policy_for_exception(network), "recovery")
        self.assertEqual(hooks._recovery_policy_for_exception(start_timeout), "recovery_cooldown")
        self.assertEqual(hooks._recovery_policy_for_exception(idle_timeout), "recovery")
        self.assertEqual(hooks._recovery_policy_for_exception(request_error), "error")
        self.assertEqual(
            hooks._recovery_policy_for_exception(account_pool_exhausted),
            "recovery_cooldown",
        )
        self.assertEqual(
            hooks._recovery_diagnostic(account_pool_exhausted)["kind"],
            "relay_capacity",
        )
        # A station wording the same failure as a failed pre-deduction with the
        # remaining credit is still a balance failure: it must cool the route
        # down instead of keeping the empty route first in every request.
        station_balance = RuntimeError(
            "OpenAIException - 预扣费额度失败, 用户剩余额度: ¥0.03, 需要预扣费额度: ¥0.05"
        )
        station_balance.status_code = 403
        self.assertEqual(hooks._recovery_policy_for_exception(station_balance), "recovery_cooldown")
        self.assertEqual(hooks._recovery_diagnostic(station_balance)["kind"], "billing")
        rejected = RuntimeError("OpenAIException - permission denied")
        rejected.status_code = 403
        # A refusal that a replay cannot change is the request-error policy, and
        # that policy cools the route down: leaving a route the key may not use
        # first in every request is what made one dead route look like an
        # outage.  The diagnostic still says which refusal it was.
        self.assertEqual(hooks._recovery_policy_for_exception(rejected), "recovery_cooldown")
        self.assertEqual(hooks._recovery_diagnostic(rejected)["kind"], "authentication")
        self.assertFalse(hooks._should_count_deployment_failure_for_cooldown(network))
        # The default now counts a rate limit against the route's cooldown, so
        # two consecutive refusals park it exactly as a 5xx does.
        self.assertTrue(hooks._should_count_deployment_failure_for_cooldown(rate_limit))
        self.assertTrue(hooks._should_count_deployment_failure_for_cooldown(start_timeout))

    def test_every_server_error_status_cools_the_route_whatever_it_says(self) -> None:
        """A 5xx is the route failing, not a rate limit in different words.

        Live 2026-11-04: one route of a five-route group answered 503
        "Service temporarily unavailable" for an hour.  The wording matched the
        rate-limit markers, so the failure was classified ``recovery``: it was
        never counted against the route, never parked, and every turn paid the
        same refused round trip before a sibling answered -- the log read as one
        obviously dead node being retried forever.

        The rule is the status, not the number and not the sentence: any
        upstream that answered 5xx answered for this route.  The same sentences
        on a 429 stay transient, because there a limit is exactly what they
        describe.  Spelling only decides a failure whose status is unreadable.
        """

        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_POLICY_SERVER_ENV, None)
        self.set_env(hooks._RECOVERY_POLICY_RATE_LIMIT_ENV, None)

        wordings = (
            "ServiceUnavailableError: OpenAIException - Service temporarily unavailable",
            "upstream overloaded",
            "high demand",
            "selected model is at capacity",
            "concurrency limit reached",
            "rate limit exceeded, retry later",
            "too many requests",
        )
        for status_code in (500, 501, 502, 503, 504, 505, 507, 520, 599):
            for wording in wordings:
                with self.subTest(status_code=status_code, wording=wording):
                    error = RuntimeError(wording)
                    error.status_code = status_code
                    self.assertEqual(
                        hooks._recovery_policy_for_exception(error),
                        "recovery_cooldown",
                    )
                    self.assertTrue(
                        hooks._should_count_deployment_failure_for_cooldown(error)
                    )

        # The same sentences on a 429 are a limit, and the default now parks a
        # limited route too: the live gateway spent 113 minutes of a 10.7-hour
        # window retrying one route that recovered four times out of 691
        # refusals.  A limit that repeats is a route the next request must not
        # try first again, so it is cooled rather than waited out.
        for wording in wordings:
            with self.subTest(status_code=429, wording=wording):
                error = RuntimeError(wording)
                error.status_code = 429
                self.assertEqual(
                    hooks._recovery_policy_for_exception(error), "recovery_cooldown"
                )
                self.assertTrue(
                    hooks._should_count_deployment_failure_for_cooldown(error)
                )

        # A wrapper with no status at all still has only its words to go on,
        # and those words are a rate-limit marker: the route is parked under the
        # same default as a 429, because the alternative is paying the same
        # refusal on every request.
        unreadable = RuntimeError("upstream overloaded, retry later")
        self.assertEqual(
            hooks._recovery_policy_for_exception(unreadable), "recovery_cooldown"
        )

    def test_recovery_policy_is_runtime_configurable(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = RuntimeError("Cannot connect to host api.example.test")
        error.status_code = 500
        self.set_env("YOUNG_ROUTER_RECOVERY_POLICY_NETWORK", "recovery_cooldown")

        self.assertEqual(hooks._recovery_policy_for_exception(error), "recovery_cooldown")
        self.assertTrue(hooks._should_count_deployment_failure_for_cooldown(error))

        rate_limit = RuntimeError("rate limit exceeded")
        rate_limit.status_code = 429
        self.set_env(hooks._RECOVERY_POLICY_RATE_LIMIT_ENV, "recovery_cooldown")
        self.assertEqual(
            hooks._recovery_policy_for_exception(rate_limit), "recovery_cooldown"
        )
        self.assertTrue(hooks._should_count_deployment_failure_for_cooldown(rate_limit))

    def test_rate_limit_that_names_a_closed_window_cools_the_route(self) -> None:
        """A 429 stating a reset beyond the horizon is a closed window.

        Retrying such a route first on every request buys only the same refusal
        and a failed round trip, which is why the route is parked.  The message
        below is an upstream's own wording: it states an absolute reset with an
        offset, not a duration.
        """

        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_POLICY_RATE_LIMIT_ENV, None)
        self.set_env(hooks._QUOTA_RESET_COOLDOWN_HORIZON_SECONDS_ENV, None)

        reset_at = datetime.now(timezone(timedelta(hours=8))) + timedelta(hours=6)
        window_closed = RuntimeError(
            "RateLimitError: upstream soft_rate (http 429): usage exceeds "
            "frequency limit, but don't worry, your usage will reset at "
            f"{reset_at.strftime('%Y-%m-%d %H:%M:%S')} UTC+8, alternatively, "
            "you can switch to the other models"
        )
        window_closed.status_code = 429

        self.assertTrue(hooks._is_quota_window_rate_limit_error(window_closed))
        self.assertEqual(
            hooks._recovery_policy_for_exception(window_closed), "recovery_cooldown"
        )
        self.assertTrue(
            hooks._should_count_deployment_failure_for_cooldown(window_closed)
        )
        self.assertEqual(
            hooks._recovery_diagnostic(window_closed)["kind"], "rate_limit_window"
        )

    def test_transient_rate_limit_keeps_a_stated_recovery_policy(self) -> None:
        """An answer that never said when it would be ready keeps what was chosen.

        Only a named reset beyond the horizon is a closed window.  A bare burst
        limit, a reset inside the horizon, and a non-429 upstream error all keep
        the configured policy -- and the default is now ``recovery_cooldown``,
        so this test states ``recovery`` explicitly to keep asserting the
        contract that matters: an operator who asks for in-place retries gets
        them, and the window rule does not quietly override that choice.
        """

        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_POLICY_RATE_LIMIT_ENV, "recovery")
        self.set_env(hooks._QUOTA_RESET_COOLDOWN_HORIZON_SECONDS_ENV, None)

        bare_burst = RuntimeError("RateLimitError: rate limit exceeded, retry later")
        bare_burst.status_code = 429
        self.assertFalse(hooks._is_quota_window_rate_limit_error(bare_burst))
        self.assertEqual(hooks._recovery_policy_for_exception(bare_burst), "recovery")
        self.assertFalse(
            hooks._should_count_deployment_failure_for_cooldown(bare_burst)
        )

        soon = datetime.now(timezone.utc) + timedelta(seconds=5)
        inside_horizon = RuntimeError(
            "rate limit: your usage will reset at "
            f"{soon.strftime('%Y-%m-%d %H:%M:%S')} UTC, retry later"
        )
        inside_horizon.status_code = 429
        self.assertFalse(hooks._is_quota_window_rate_limit_error(inside_horizon))
        self.assertEqual(
            hooks._recovery_policy_for_exception(inside_horizon), "recovery"
        )

        server_error = RuntimeError(
            "rate limit reset at 2030-01-01 00:00:00 UTC"
        )
        server_error.status_code = 500
        self.assertFalse(hooks._is_quota_window_rate_limit_error(server_error))
        # Rate-limit wording on a 5xx is a server that failed, not a limit:
        # only a 429 without a stated reset stays a transient wait.
        self.assertEqual(
            hooks._recovery_policy_for_exception(server_error), "recovery_cooldown"
        )

    def test_quota_reset_horizon_is_runtime_configurable(self) -> None:
        """The horizon is the boundary between busy-now and closed-window."""

        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_POLICY_RATE_LIMIT_ENV, None)
        reset_at = datetime.now(timezone.utc) + timedelta(seconds=120)
        error = RuntimeError(
            "rate limit: your usage will reset at "
            f"{reset_at.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        error.status_code = 429

        self.set_env(hooks._QUOTA_RESET_COOLDOWN_HORIZON_SECONDS_ENV, None)
        self.assertTrue(hooks._is_quota_window_rate_limit_error(error))
        self.set_env(hooks._QUOTA_RESET_COOLDOWN_HORIZON_SECONDS_ENV, "600")
        self.assertFalse(hooks._is_quota_window_rate_limit_error(error))
        self.set_env(hooks._QUOTA_RESET_COOLDOWN_HORIZON_SECONDS_ENV, "0")
        self.assertTrue(hooks._is_quota_window_rate_limit_error(error))

    def test_explicit_rate_limit_policy_outranks_the_window_rule(self) -> None:
        """A user's own 429 policy is never refined by the window rule."""

        hooks, _proxy_server = load_hook_module()
        reset_at = datetime.now(timezone.utc) + timedelta(hours=6)
        error = RuntimeError(
            "rate limit: your usage will reset at "
            f"{reset_at.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        error.status_code = 429

        self.set_env(hooks._RECOVERY_POLICY_RATE_LIMIT_ENV, "error")
        self.assertEqual(hooks._recovery_policy_for_exception(error), "error")
        self.set_env(hooks._RECOVERY_POLICY_RATE_LIMIT_ENV, "recovery_cooldown")
        self.assertEqual(
            hooks._recovery_policy_for_exception(error), "recovery_cooldown"
        )
        self.set_env(hooks._RECOVERY_POLICY_RATE_LIMIT_ENV, None)
        self.assertEqual(
            hooks._recovery_policy_for_exception(error), "recovery_cooldown"
        )

    def test_quota_reset_time_parses_offsets_and_rejects_nonsense(self) -> None:
        """The stated offset is honored; unreadable text is never guessed.

        A station that answers in local time would otherwise be read as UTC, and
        the wait would be wrong by the offset. An unreadable statement must
        return None rather than fall back to a duration the message never gave.
        """

        hooks, _proxy_server = load_hook_module()
        for label, tz in (
            ("UTC+8", timezone(timedelta(hours=8))),
            ("UTC-5", timezone(timedelta(hours=-5))),
            ("Z", timezone.utc),
        ):
            with self.subTest(offset=label):
                wall_clock = datetime(2030, 10, 14, 0, 24, 9)
                seconds = hooks._quota_reset_absolute_seconds(
                    f"reset at {wall_clock.strftime('%Y-%m-%d %H:%M:%S')} {label}"
                )
                expected = (
                    wall_clock.replace(tzinfo=tz) - datetime.now(timezone.utc)
                ).total_seconds()
                self.assertIsNotNone(seconds)
                self.assertAlmostEqual(seconds, expected, delta=1.0)

        for text in (
            "reset at tomorrow",
            "reset at 2030-13-45 99:99:99 UTC",
            "rate limit exceeded",
        ):
            with self.subTest(text=text):
                self.assertIsNone(hooks._quota_reset_absolute_seconds(text))

        # A reset already in the past never extends a cooldown.
        self.assertEqual(
            hooks._quota_reset_absolute_seconds("reset at 2020-01-01 00:00:00 UTC"),
            0.0,
        )

        # A duration-only statement is still read, because it states the wait.
        duration = RuntimeError("rate limit: your usage will reset, retry after 30 minutes")
        duration.status_code = 429
        self.assertEqual(hooks._exception_quota_reset_seconds(duration), 1800.0)
        self.assertTrue(hooks._is_quota_window_rate_limit_error(duration))

    def test_deterministic_request_error_never_enters_recovery_even_when_marked_failed(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = RuntimeError("OpenAIException invalid_request_error: invalid input[89].id")
        error.status_code = 400
        error.failed_deployment_id = "route-a"

        self.assertEqual(hooks._recovery_policy_for_exception(error), "error")
        self.assertFalse(hooks._is_route_recovery_poll_error(error))

    def test_structured_compaction_body_capacity_error_uses_only_route_failover(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = RuntimeError(
            "OpenAIException - invalid request: request body storage capacity exhausted"
        )
        error.status_code = 400
        structured_compaction_request = {
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }
        ordinary_request = {
            **structured_compaction_request,
            "input": structured_compaction_request["input"][:-1],
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
        }

        self.assertTrue(hooks._is_upstream_request_body_storage_capacity_error(error))
        self.assertFalse(hooks._is_priority_deployment_failover_error(error))
        self.assertTrue(
            hooks._is_request_scoped_priority_deployment_failover_error(
                error,
                structured_compaction_request,
            )
        )
        self.assertFalse(
            hooks._is_request_scoped_priority_deployment_failover_error(
                error,
                ordinary_request,
            )
        )
        self.assertEqual(hooks._recovery_policy_for_exception(error), "error")
        self.assertFalse(
            hooks._should_return_route_recovery_stream(
                error,
                structured_compaction_request,
            )
        )
        self.assertEqual(
            hooks._trace_exception(error)["reason"],
            "upstream-request-body-capacity",
        )

    def test_unsupported_compaction_advances_to_other_candidate_routes(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = hooks.CodexCompactionCapabilityUnsupportedError(
            deployment_id="route-a",
            route_key="provider / openai/default-chat / key=a / order=1",
        )
        request = {
            "model": "default-chat",
            "input": [{"type": "compaction_trigger", "id": "compact-now"}],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }

        # The capability rejection belongs to the probed deployment: while
        # another candidate deployment exists the request advances to it.
        self.assertTrue(hooks._is_priority_deployment_failover_error(error))
        self.assertTrue(
            hooks._is_request_scoped_priority_deployment_failover_error(error, request)
        )
        hooks._sync_failed_deployment_exclusions(request, error)
        self.assertEqual(["route-a"], request.get("_excluded_deployment_ids"))
        # A capability failure is deterministic for this deployment, so it is
        # never sent into cooldown accounting or the long recovery polling
        # loop. After every candidate is excluded the caller surfaces the
        # explicit protocol error instead.
        self.assertEqual(hooks._recovery_policy_for_exception(error), "error")
        self.assertFalse(hooks._should_return_route_recovery_stream(error, request))
        self.assertEqual(
            hooks._trace_exception(error)["reason"],
            "codex-compaction-unsupported",
        )

    def test_unknown_custom_tool_type_is_not_a_deployment_failover(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = RuntimeError("OpenAIException invalid_request_error: unknown tool type: custom")
        error.status_code = 400
        error.failed_deployment_id = "route-a"

        self.assertFalse(hooks._is_deployment_compatible_bad_request_error(error))
        self.assertFalse(hooks._is_priority_deployment_failover_error(error))
        self.assertEqual(hooks._recovery_policy_for_exception(error), "error")

    def test_upstream_high_risk_rejection_never_enters_recovery(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = RuntimeError(
            "OpenAIException - the request was rejected because it was considered high risk"
        )
        error.status_code = 400
        error.failed_deployment_id = "route-a"

        self.assertTrue(hooks._is_terminal_prompt_or_policy_error(error))
        self.assertFalse(hooks._is_deployment_compatible_bad_request_error(error))
        self.assertEqual(hooks._recovery_policy_for_exception(error), "error")
        self.assertFalse(hooks._is_route_recovery_poll_error(error))

    async def test_filter_deployments_keeps_image_tool_candidates_for_runtime_probe(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        deployments = [
            {
                "litellm_params": {"model": "openai/dynamic-text"},
                "model_info": {
                    "id": "dynamic-a",
                    "provider": "any-provider",
                    "supports_responses_image_generation_tool": False,
                },
            },
            {
                "litellm_params": {"model": "openai/dynamic-image"},
                "model_info": {
                    "id": "dynamic-b",
                    "provider": "another-provider",
                    "supports_responses_image_generation_tool": True,
                },
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=[],
            request_kwargs={"tools": [{"type": "image_generation"}]},
        )

        self.assertEqual(filtered, deployments)

    async def test_filter_deployments_keeps_candidates_without_current_image_capability(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        deployments = [
            {
                "litellm_params": {"model": "openai/dynamic-text"},
                "model_info": {"id": "dynamic-a"},
            },
            {
                "litellm_params": {"model": "openai/dynamic-image"},
                "model_info": {"id": "dynamic-b"},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=[],
            request_kwargs={"tools": [{"type": "image_generation"}]},
        )

        self.assertEqual(filtered, deployments)

    async def test_filter_deployments_preserves_user_surface_order_for_codex_tools(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        chat_only = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://api.backup.example/v1",
                "order": 2,
            },
            "model_info": {
                "id": "chat-only",
                "provider": "backup_provider",
                "api_key_name": "x-plus",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat", "anthropic"],
            },
        }
        responses = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://headers.example/v1",
                "order": 2,
            },
            "model_info": {
                "id": "responses",
                "provider": "compat_provider",
                "api_key_name": "x-plus",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/responses"],
            },
        }
        deployments = [chat_only, responses]

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=[],
            request_kwargs={
                "call_type": "aresponses",
                "stream": True,
                "client_metadata": {"session_id": "codex-thread"},
                "tools": [
                    {"type": "function", "name": "exec_command"},
                    {"type": "custom", "name": "apply_patch"},
                    {"type": "web_search"},
                ],
            },
        )

        self.assertEqual(filtered, deployments)

    async def test_filter_deployments_keeps_chat_surface_when_no_responses_candidate(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        deployments = [
            {
                "litellm_params": {
                    "model": "openai/default-chat",
                    "api_base": "https://api.backup.example/v1",
                    "order": 2,
                },
                "model_info": {
                    "id": "chat-only",
                    "provider": "backup_provider",
                    "api_key_name": "x-plus",
                    "upstream_url_surface": "openai/chat",
                    "supported_upstream_url_surfaces": ["openai/chat"],
                },
            }
        ]

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=[],
            request_kwargs={
                "call_type": "aresponses",
                "stream": True,
                "client_metadata": {"session_id": "codex-thread"},
                "tools": [{"type": "function", "name": "exec_command"}],
            },
        )

        self.assertEqual(filtered, deployments)

    async def test_no_healthy_deployments_bad_request_is_route_exhaustion(self) -> None:
        hooks, _ = load_hook_module()

        class BadRequestError(Exception):
            status_code = 400

        error = BadRequestError(
            "You passed in model=balanced-chat. There are no healthy deployments for this model. "
            "Received Model Group=balanced-chat Available Model Group Fallbacks=None"
        )

        self.assertTrue(hooks._is_no_deployments_available_error(error))
        self.assertTrue(hooks._is_route_recovery_poll_error(error))

    def test_mark_exception_defers_recoverable_route_exclusion_until_budget_exhausts(self) -> None:
        hooks, _ = load_hook_module()
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        request_kwargs = {
            "_excluded_deployment_ids": ["already-failed"],
            "model_info": {"id": "newly-failed", "order": 2},
        }

        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        self.assertEqual(error.failed_deployment_id, "newly-failed")
        self.assertEqual(error.failed_deployment_order, 2)
        self.assertEqual(request_kwargs["_excluded_deployment_ids"], ["already-failed"])
        self.assertEqual(error.num_retries, 0)

    def test_mark_exception_defers_timeout_route_exclusion_until_budget_exhausts(self) -> None:
        hooks, _ = load_hook_module()
        error = RuntimeError("upstream gateway timeout after 60s")
        error.status_code = 504
        request_kwargs = {
            "_excluded_deployment_ids": ["already-failed"],
            "model_info": {
                "id": "chatroute",
                "order": 1,
                "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
            },
        }

        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        self.assertEqual(error.failed_deployment_id, "chatroute")
        self.assertEqual(error.failed_deployment_order, 1)
        self.assertEqual(request_kwargs["_excluded_deployment_ids"], ["already-failed"])
        self.assertFalse(hasattr(error, "excluded_deployment_ids"))
        self.assertTrue(hooks._should_retry_same_deployment_before_fallback(error))

    def test_mark_exception_defers_rate_limit_route_exclusion_until_budget_exhausts(self) -> None:
        hooks, _ = load_hook_module()
        error = RuntimeError("upstream 429 rate limit exceeded; retry after 10 seconds")
        error.status_code = 429
        request_kwargs = {
            "_excluded_deployment_ids": ["already-failed"],
            "model_info": {
                "id": "chatroute",
                "order": 1,
                "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
            },
        }

        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        self.assertEqual(error.failed_deployment_id, "chatroute")
        self.assertEqual(error.failed_deployment_order, 1)
        self.assertEqual(request_kwargs["_excluded_deployment_ids"], ["already-failed"])
        self.assertFalse(hasattr(error, "excluded_deployment_ids"))
        self.assertTrue(hooks._should_retry_same_deployment_before_fallback(error))

    async def test_deployment_cooldown_respects_configured_failure_threshold(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "3")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
            {"litellm_params": {"model": "openai/x-pro"}, "model_info": {"id": "x-pro"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        for _ in range(2):
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments[1:])

    def test_deployment_cooldown_is_shared_across_client_protocols(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        with tempfile.TemporaryDirectory() as directory:
            self.set_env(
                hooks._DEPLOYMENT_COOLDOWN_FILE_ENV,
                str(Path(directory) / "deployment-cooldowns.json"),
            )
            deployment = {
                "litellm_params": {"model": "provider/default-route"},
                "model_info": {"id": "shared-route"},
            }
            failed_request = {
                "model": "default-chat",
                "input": [{"role": "user", "content": "Continue."}],
                "stream": True,
                "proxy_server_request": {"path": "/v1/responses"},
                "litellm_params": deployment["litellm_params"],
                "model_info": deployment["model_info"],
            }
            failure = RuntimeError("temporary upstream failure")
            failure.status_code = 503

            hooks._mark_exception_for_deployment_failover(failure, failed_request)

            requests = {
                "responses": {
                    "model": "default-chat",
                    "input": [{"role": "user", "content": "Continue."}],
                    "stream": True,
                    "proxy_server_request": {"path": "/v1/responses"},
                },
                "chat_completions": {
                    "model": "default-chat",
                    "messages": [{"role": "user", "content": "Continue."}],
                    "stream": True,
                    "proxy_server_request": {"path": "/v1/chat/completions"},
                },
                "anthropic_messages": {
                    "model": "default-chat",
                    "messages": [{"role": "user", "content": "Continue."}],
                    "max_tokens": 64,
                    "stream": True,
                    "proxy_server_request": {"path": "/v1/messages"},
                },
            }
            for protocol, request_data in requests.items():
                with self.subTest(protocol=protocol):
                    available, cooled, filtered = hooks._with_active_deployment_cooldowns(
                        [deployment],
                        request_kwargs=request_data,
                    )
                    self.assertEqual(available, [])
                    self.assertEqual(len(cooled), 1)
                    self.assertTrue(filtered)

            successful_messages_request = {
                **requests["anthropic_messages"],
                "litellm_params": deployment["litellm_params"],
                "model_info": deployment["model_info"],
            }
            hooks._record_deployment_success_for_cooldown(
                successful_messages_request
            )
            available, cooled, filtered = hooks._with_active_deployment_cooldowns(
                [deployment],
                request_kwargs=requests["responses"],
            )
            self.assertEqual(available, [deployment])
            self.assertEqual(cooled, [])
            self.assertFalse(filtered)

    async def test_a_relay_503_is_parked_instead_of_tried_first_again(self) -> None:
        """The live shape: one relay answered 503, and every turn paid it first.

        Live 2026-11-04: the first route of a five-route group answered 503
        ``Service temporarily unavailable`` on every turn for an hour.  Its
        sentence matched the rate-limit wording, so nothing counted against the
        route, nothing cooled it down, and every request spent a refused round
        trip there before a sibling answered.  The second refusal has to park
        it, and the candidate pool of the next request must not contain it.
        """

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        for _attempt in range(hooks._deployment_cooldown_failure_threshold()):
            error = RuntimeError(
                "ServiceUnavailableError: OpenAIException - Service temporarily unavailable"
            )
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_defaults_to_two_failures(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    def test_deployment_cooldown_defaults_split_ordinary_and_compaction_writes(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED_ENV, None)
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED_ENV, None)

        ordinary_request = {"model": "default-chat", "input": [{"role": "user", "content": "work"}]}
        compaction_request = {
            "model": "default-chat",
            "input": [{"type": "compaction_trigger", "id": "compact-now"}],
        }

        self.assertTrue(
            hooks._deployment_cooldown_recording_enabled_for_request(ordinary_request)
        )
        self.assertFalse(
            hooks._deployment_cooldown_recording_enabled_for_request(compaction_request)
        )

    def test_compaction_cooldown_setting_keeps_one_shared_pool(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED_ENV, "0")
        hooks._DEPLOYMENT_COOLDOWNS.clear()
        self.addCleanup(hooks._DEPLOYMENT_COOLDOWNS.clear)
        deployment = {
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "cooldown-shared-route"},
        }
        ordinary_request = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "cooldown-shared-route"},
        }
        compaction_request = {
            "model": "default-chat",
            "input": [{"type": "compaction_trigger", "id": "compact-now"}],
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "cooldown-shared-route"},
        }

        ordinary_error = RuntimeError("temporary upstream failure")
        ordinary_error.status_code = 503
        hooks._mark_exception_for_deployment_failover(ordinary_error, ordinary_request)
        cooldown_key = "id:cooldown-shared-route"
        self.assertIn(cooldown_key, hooks._DEPLOYMENT_COOLDOWNS)

        available, cooled, filtered = hooks._with_active_deployment_cooldowns(
            [deployment], request_kwargs=compaction_request
        )
        self.assertEqual(available, [])
        self.assertEqual(len(cooled), 1)
        self.assertTrue(filtered)

        compaction_error = RuntimeError("temporary upstream failure")
        compaction_error.status_code = 503
        hooks._mark_exception_for_deployment_failover(compaction_error, compaction_request)
        self.assertEqual(hooks._DEPLOYMENT_COOLDOWNS[cooldown_key]["failures"], 1)

        self.set_env(hooks._DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED_ENV, "1")
        enabled_compaction_error = RuntimeError("temporary upstream failure")
        enabled_compaction_error.status_code = 503
        hooks._mark_exception_for_deployment_failover(
            enabled_compaction_error, compaction_request
        )
        self.assertEqual(hooks._DEPLOYMENT_COOLDOWNS[cooldown_key]["failures"], 2)

    async def test_deployment_cooldown_persists_across_worker_memory(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        with tempfile.TemporaryDirectory() as temp_dir:
            self.set_env(
                hooks._DEPLOYMENT_COOLDOWN_FILE_ENV,
                str(Path(temp_dir) / "deployment-cooldowns.json"),
            )
            route_key = "backup_provider / openai/default-chat / host=api.backup.example / key=x-plus / order=2"
            deployments = [
                {
                    "litellm_params": {
                        "model": "openai/default-chat",
                        "api_base": "https://api.backup.example/v1",
                        "order": 2,
                    },
                    "model_info": {
                        "id": "stable-backup_provider",
                        "provider": "backup_provider",
                        "api_key_name": "x-plus",
                        "route_key": route_key,
                    },
                },
                {
                    "litellm_params": {
                        "model": "openai/default-chat",
                        "api_base": "https://headers.example/v1",
                        "order": 2,
                    },
                    "model_info": {
                        "id": "healthy-compat_provider",
                        "provider": "compat_provider",
                        "api_key_name": "x-plus",
                    },
                },
            ]
            request_kwargs = {
                "model": "default-chat",
                "litellm_params": {
                    "model": "openai/default-chat",
                    "api_base": "https://api.backup.example/v1",
                    "order": 2,
                },
                "model_info": {
                    "id": "stable-backup_provider",
                    "provider": "backup_provider",
                    "api_key_name": "x-plus",
                    "route_key": route_key,
                },
            }

            for _ in range(2):
                error = RuntimeError("insufficient account balance")
                error.status_code = 403
                hooks._mark_exception_for_deployment_failover(error, request_kwargs)

            cooldown_payload = json.loads(
                Path(temp_dir, "deployment-cooldowns.json").read_text(encoding="utf-8")
            )
            cooldown_state = cooldown_payload["cooldowns"]["id:stable-backup_provider"]
            self.assertEqual(cooldown_state["model_group"], "default-chat")
            self.assertEqual(cooldown_state["provider"], "backup_provider")
            self.assertEqual(cooldown_state["upstream_model"], "openai/default-chat")
            self.assertEqual(cooldown_state["api_base_host"], "api.backup.example")
            self.assertEqual(cooldown_state["deployment_order"], 2)

            hooks._DEPLOYMENT_COOLDOWNS.clear()

            filtered = await hook.async_filter_deployments(
                "default-chat",
                deployments,
                messages=None,
                request_kwargs={},
            )
            self.assertEqual(filtered, [deployments[1]])

    async def test_read_only_cooldown_reads_do_not_rewrite_the_shared_file(self) -> None:
        """Reading the shared pool never churns its file; a real change still writes."""

        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "deployment-cooldowns.json"
            self.set_env(hooks._DEPLOYMENT_COOLDOWN_FILE_ENV, str(path))
            deployment = {
                "litellm_params": {
                    "model": "openai/default-chat",
                    "api_base": "https://api.backup.example/v1",
                    "order": 2,
                },
                "model_info": {
                    "id": "stable-backup_provider",
                    "provider": "backup_provider",
                    "api_key_name": "x-plus",
                },
            }
            request_kwargs = {
                "model": "default-chat",
                "litellm_params": deployment["litellm_params"],
                "model_info": deployment["model_info"],
            }

            available, cooled, filtered = hooks._with_active_deployment_cooldowns(
                [deployment],
                request_kwargs=request_kwargs,
            )
            self.assertEqual([deployment], available)
            self.assertEqual([], cooled)
            self.assertFalse(filtered)
            written = path.stat().st_mtime_ns

            for _ in range(3):
                hooks._with_active_deployment_cooldowns(
                    [deployment],
                    request_kwargs=request_kwargs,
                )

            self.assertEqual(written, path.stat().st_mtime_ns)

            error = RuntimeError("insufficient account balance")
            error.status_code = 403
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

            self.assertNotEqual(written, path.stat().st_mtime_ns)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                1,
                payload["cooldowns"]["id:stable-backup_provider"]["failures"],
            )

    async def test_deployment_cooldown_success_clears_failure_count(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "3")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-pro"}, "model_info": {"id": "x-pro"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        for _ in range(3):
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

        await hook.async_log_success_event(
            request_kwargs,
            {"ok": True},
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

    async def test_stream_start_does_not_clear_deployment_cooldown(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        request_kwargs = {
            "model": "default-chat",
            "stream": True,
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)
        self.assertTrue(hooks._DEPLOYMENT_COOLDOWNS)

        await hook.async_log_success_event(
            request_kwargs,
            {"type": "response.created", "response": {"status": "in_progress"}},
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )
        await hook.async_log_stream_event(
            request_kwargs,
            {"type": "response.created", "response": {"status": "in_progress"}},
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )
        self.assertTrue(hooks._DEPLOYMENT_COOLDOWNS)

        await hook.async_log_stream_event(
            request_kwargs,
            {"type": "response.completed", "response": {"status": "completed"}},
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )
        self.assertFalse(hooks._DEPLOYMENT_COOLDOWNS)

    async def test_a_completed_native_stream_clears_the_failure_count(self) -> None:
        """A streamed answer is a success; it must reset the route's streak.

        The cooldown row said "连续失败 5 次" while the request log showed
        several completed streams to the same route in between.  Only
        Responses-style completion was recognized, so native Messages and Chat
        streams never cleared the count and "consecutive" was really
        "cumulative since the last expiry".
        """

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        request_kwargs = {
            "model": "default-chat",
            "stream": True,
            "litellm_params": {"model": "anthropic/claude-x"},
            "model_info": {"id": "stream-route"},
        }
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)
        self.assertTrue(hooks._DEPLOYMENT_COOLDOWNS)

        now = datetime.now(timezone.utc)
        await hook.async_log_stream_event(
            request_kwargs,
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            now,
            now,
        )
        self.assertFalse(hooks._DEPLOYMENT_COOLDOWNS)

    async def test_a_completed_stream_success_clears_the_failure_count(self) -> None:
        """The terminal success callback carries a completed stream, not a chunk."""

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        request_kwargs = {
            "model": "default-chat",
            "stream": True,
            "litellm_params": {"model": "anthropic/claude-x"},
            "model_info": {"id": "stream-route"},
        }
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)
        self.assertTrue(hooks._DEPLOYMENT_COOLDOWNS)

        now = datetime.now(timezone.utc)
        await hook.async_log_success_event(
            {
                **request_kwargs,
                "async_complete_streaming_response": {"type": "message"},
            },
            {"type": "message"},
            now,
            now,
        )
        self.assertFalse(hooks._DEPLOYMENT_COOLDOWNS)

    async def test_deployment_cooldown_does_not_count_sanitized_wrapper(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "3")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-pro"}, "model_info": {"id": "x-pro"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        for _ in range(2):
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        sanitized = RuntimeError("sanitized wrapper")
        sanitized.status_code = 503
        setattr(sanitized, hooks._SANITIZED_UPSTREAM_ROUTE_FAILURE_ATTR, True)
        hooks._mark_exception_for_deployment_failover(sanitized, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_filters_all_cooled_candidates_globally(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "3")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]

        for deployment in deployments:
            request_kwargs = {
                "model": "default-chat",
                "litellm_params": deployment["litellm_params"],
                "model_info": deployment["model_info"],
            }
            for _ in range(3):
                error = RuntimeError("temporary upstream failure")
                error.status_code = 503
                hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [])

    async def test_route_recovery_does_not_half_open_cooled_candidates(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]

        for deployment in deployments:
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(
                error,
                {
                    "model": "default-chat",
                    "litellm_params": deployment["litellm_params"],
                    "model_info": deployment["model_info"],
                },
            )

        recovery_request = {
            "litellm_metadata": {
                hooks._ROUTE_RECOVERY_POLL_METADATA_KEY: True,
            },
        }
        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs=recovery_request,
        )
        self.assertEqual(filtered, [])

        available, cooled, cooldown_filtered = hooks._with_active_deployment_cooldowns(
            deployments,
            request_kwargs=recovery_request,
        )
        self.assertTrue(cooldown_filtered)
        self.assertEqual(available, [])
        self.assertEqual(len(cooled), 2)
        self.assertFalse(any(entry.get("half_open_probe") is True for entry in cooled))

    async def test_interactive_route_recovery_waits_when_all_candidates_are_cooling_down(self) -> None:
        hooks, proxy_server = load_hook_module()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        recovery_state_path = Path(temp_dir.name) / "route-recovery-state.json"
        self.set_env(hooks._ROUTE_RECOVERY_STATE_FILE_ENV, str(recovery_state_path))
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "0.05")
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.15")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.005")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments if model_name == "default-chat" else []

        proxy_server.llm_router = Router()
        for deployment in deployments:
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(
                error,
                {
                    "model": "default-chat",
                    "litellm_params": deployment["litellm_params"],
                    "model_info": deployment["model_info"],
                },
            )

        request_data = {
            "model": "default-chat",
            "stream": True,
            "input": [{"role": "user", "content": "Continue."}],
            "_target_order": 1,
            "_excluded_deployment_ids": ["x-cheap", "x-plus"],
        }
        self.assertTrue(
            hooks._route_recovery_poll_cooldown_wait(
                request_data,
                ignore_constraints=True,
            )
        )

        calls = []
        first_cooldown_until = None

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered after cooldown"}
            yield {"type": "response.completed", "response": {"id": "resp-after-cooldown"}}

        async def upstream_after_cooldown(**payload):
            self.assertIsNotNone(first_cooldown_until)
            assert first_cooldown_until is not None
            self.assertGreaterEqual(time.time(), first_cooldown_until)
            calls.append(payload.copy())
            return recovered_stream()

        proxy_server.llm_router.aresponses = upstream_after_cooldown
        failure = RuntimeError("temporary upstream failure")
        failure.status_code = 503
        failure.failed_deployment_id = "x-cheap"
        failure.failed_deployment_order = 1
        request_data["_route_recovery_ignore_local_constraints"] = True
        stream = hooks._stream_route_recovery_poll(request_data, failure)
        first_chunk = jsonable_stream_chunk(await anext(stream))
        self.assertTrue(hooks._is_route_recovery_sse_keepalive(first_chunk))
        self.assertEqual(first_chunk["metadata"]["phase"], "cooldown")
        self.assertEqual(calls, [])
        waiting_state = json.loads(recovery_state_path.read_text(encoding="utf-8"))
        record = next(iter(waiting_state["recoveries"].values()))
        self.assertEqual(record["status"], "waiting")
        self.assertGreater(record["cooldown_until"], time.time())
        self.assertGreater(record["cooldown_remaining_seconds"], 0)
        first_cooldown_until = record["cooldown_until"]

        await asyncio.sleep(0.01)
        next_chunk = jsonable_stream_chunk(await anext(stream))
        self.assertTrue(hooks._is_route_recovery_sse_keepalive(next_chunk))
        self.assertEqual(next_chunk["metadata"]["phase"], "cooldown")
        self.assertEqual(calls, [])

        chunks = [first_chunk, next_chunk]
        async for chunk in stream:
            chunks.append(jsonable_stream_chunk(chunk))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("_excluded_deployment_ids", calls[0])
        self.assertNotIn("_target_order", calls[0])
        self.assertTrue(any(hooks._is_route_recovery_sse_keepalive(chunk) for chunk in chunks))
        self.assertIn(
            {"type": "response.output_text.delta", "delta": "recovered after cooldown"},
            chunks,
        )
        self.assertEqual(chunks[-1]["type"], "response.completed")

    async def test_stream_fallback_recovery_waits_for_a_shared_cooldown(self) -> None:
        hooks, proxy_server = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "0.05")
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.15")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.005")
        deployments = [
            {"litellm_params": {"order": 1}, "model_info": {"id": "route-a"}},
            {"litellm_params": {"order": 2}, "model_info": {"id": "route-b"}},
        ]

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments if model_name == "default-chat" else []

            async def aresponses(self, **payload):
                self_calls.append((time.time(), payload.copy()))
                async def recovered_stream():
                    yield {"type": "response.output_text.delta", "delta": "recovered"}
                    yield {"type": "response.completed", "response": {"id": "resp-recovered"}}
                return recovered_stream()

        proxy_server.llm_router = Router()
        for deployment in deployments:
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(
                error,
                {
                    "model": "default-chat",
                    "litellm_params": deployment["litellm_params"],
                    "model_info": deployment["model_info"],
                },
            )

        available, cooled, _ = hooks._with_active_deployment_cooldowns(
            deployments,
            request_kwargs={"model": "default-chat"},
        )
        self.assertEqual(available, [])
        cooldown_until = min(entry["cooldown_until"] for entry in cooled)
        self_calls = []

        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "_target_order": 1,
            "_excluded_deployment_ids": ["route-a", "route-b"],
        }
        initial_failure = RuntimeError("No deployments available for selected model")
        initial_failure.status_code = 503
        initial_failure.failed_deployment_id = "route-a"
        initial_failure.failed_deployment_order = 1
        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._yield_streaming_error_fallback_or_raise(
                request_data,
                initial_failure,
            )
        ]

        self.assertEqual(len(self_calls), 1)
        self.assertGreaterEqual(self_calls[0][0], cooldown_until)
        self.assertNotIn("_target_order", self_calls[0][1])
        self.assertNotIn("_excluded_deployment_ids", self_calls[0][1])
        self.assertTrue(any(hooks._is_route_recovery_sse_keepalive(chunk) for chunk in chunks))
        self.assertEqual(chunks[-1]["type"], "response.completed")

    async def test_route_recovery_prefers_healthy_peer_over_cooled_candidates(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(
            error,
            {
                "model": "default-chat",
                "litellm_params": deployments[0]["litellm_params"],
                "model_info": deployments[0]["model_info"],
            },
        )

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={
                "litellm_metadata": {
                    hooks._ROUTE_RECOVERY_POLL_METADATA_KEY: True,
                },
            },
        )

        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_expires_after_ttl(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "0.01")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

        await asyncio.sleep(0.02)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

    async def test_deployment_cooldown_does_not_count_request_errors_but_counts_rate_limits(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        errors = []
        context_error = RuntimeError(
            "This model's maximum context length is 4096 tokens, but your prompt contains 5000 tokens."
        )
        context_error.status_code = 400
        errors.append(context_error)

        request_shape_error = RuntimeError(
            "OpenAIException invalid_request_error: system messages are not allowed"
        )
        request_shape_error.status_code = 400
        errors.append(request_shape_error)

        rate_limit_error = RuntimeError("rate limit exceeded; retry after 10 seconds")
        rate_limit_error.status_code = 429

        for error in errors:
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        # A rate limit is not a request-shaped error: the default counts it
        # against the route, so it parks the route exactly as a server error
        # would.  Request and context errors stay uncounted (asserted above).
        hooks._mark_exception_for_deployment_failover(rate_limit_error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_counts_server_timeouts_but_not_stream_idle_errors(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        timeout_error = RuntimeError("upstream gateway timeout")
        timeout_error.status_code = 504
        hooks._mark_exception_for_deployment_failover(timeout_error, request_kwargs)

        long_wait_error = RuntimeError("upstream returned 500 after 60s")
        long_wait_error.status_code = 500
        hooks._mark_exception_for_deployment_failover(
            long_wait_error,
            {**request_kwargs, "duration_ms": 60000},
        )

        stream_idle_error = TimeoutError("stream idle timeout")
        stream_idle_error.status_code = 504
        stream_idle_error.body = {"reason": "stream_idle_timeout"}
        hooks._mark_exception_for_deployment_failover(stream_idle_error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_does_not_count_network_connectivity_errors(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        for message in (
            "InternalServerError: OpenAIException - Cannot connect to host api.example.test:443 ssl:default",
            "InternalServerError: OpenAIException - Server disconnected",
        ):
            error = RuntimeError(message)
            error.status_code = 500
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)
        self.assertNotIn("_excluded_deployment_ids", request_kwargs)

    def test_network_connectivity_error_is_retryable_but_not_hard_deployment_failure(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "InternalServerError: OpenAIException - Cannot connect to host api.example.test:443 ssl:default"
        )
        exc.status_code = 500

        self.assertTrue(hooks._exception_indicates_network_connectivity_error(exc))
        self.assertTrue(hooks._is_route_recovery_poll_error(exc))
        self.assertTrue(hooks._should_sanitize_final_upstream_route_error(exc))
        self.assertTrue(hooks._should_retry_same_deployment_before_fallback(exc))
        self.assertFalse(hooks._should_count_deployment_failure_for_cooldown(exc))
        self.assertEqual(hooks._trace_exception(exc)["reason"], "upstream-network-connectivity")

        class ReadError(Exception):
            pass

        disconnected = ReadError("connection closed.")
        self.assertTrue(hooks._exception_indicates_network_connectivity_error(disconnected))
        self.assertTrue(hooks._is_priority_deployment_failover_error(disconnected))
        self.assertTrue(hooks._should_retry_same_deployment_before_fallback(disconnected))

        sending_request = RuntimeError("Connection failed: error sending request")
        self.assertTrue(
            hooks._is_network_recovery_exception(sending_request)
        )
        self.assertEqual(
            hooks._recovery_policy_for_exception(sending_request),
            "recovery",
        )

    async def test_deployment_cooldown_does_not_count_stream_start_timeout_after_chunks(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        saw_chunk_error = TimeoutError("stream did not start before timeout")
        saw_chunk_error.status_code = 504
        saw_chunk_error.body = {"reason": "stream_start_timeout", "saw_chunk": True}
        hooks._mark_exception_for_deployment_failover(saw_chunk_error, request_kwargs)

        buffered_chunk_error = TimeoutError("stream did not start before timeout")
        buffered_chunk_error.status_code = 504
        buffered_chunk_error.body = {"reason": "stream_start_timeout", "buffered_chunks": 104}
        hooks._mark_exception_for_deployment_failover(buffered_chunk_error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

    async def test_deployment_cooldown_counts_local_stream_start_timeout(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
            "duration_ms": 526918,
        }

        for _ in range(2):
            error = TimeoutError("stream did not start before timeout")
            error.status_code = 504
            error.body = {"reason": "stream_start_timeout"}
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_counts_quota_or_auth_failures(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        error = RuntimeError("insufficient_quota: account balance exhausted")
        error.status_code = 403
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_counts_long_quota_or_auth_failures(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
            "duration_ms": 60000,
        }

        error = RuntimeError("insufficient_quota: account balance exhausted after 60 seconds")
        error.status_code = 403
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_does_not_cross_deployment_ids_with_same_route_key(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        route_key = "compat_provider / openai/default-chat / key=x-plus / order=2"
        deployments = [
            {
                "litellm_params": {"model": "openai/default-chat", "order": 2},
                "model_info": {"id": "new-route-id-a", "route_key": route_key},
            },
            {
                "litellm_params": {"model": "openai/default-chat", "order": 2},
                "model_info": {"id": "new-route-id-b", "route_key": route_key},
            },
            {
                "litellm_params": {"model": "openai/default-chat", "order": 3},
                "model_info": {
                    "id": "healthy-pro",
                    "route_key": "compat_provider / openai/default-chat / key=x-pro / order=3",
                },
            },
        ]

        for _ in range(2):
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(
                error,
                {
                    "model": "default-chat",
                    "litellm_params": {"model": "openai/default-chat", "order": 2},
                    "model_info": {"id": "new-route-id-a", "route_key": route_key},
                },
            )

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1], deployments[2]])

    async def test_deployment_cooldown_uses_route_key_when_deployment_id_missing(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        route_key = "legacy / openai/default-chat / key=x-plus / order=2"
        deployments = [
            {
                "litellm_params": {"model": "openai/default-chat", "order": 2},
                "model_info": {"route_key": route_key},
            },
            {
                "litellm_params": {"model": "openai/default-chat", "order": 3},
                "model_info": {"route_key": "legacy / openai/default-chat / key=x-pro / order=3"},
            },
        ]

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(
            error,
            {
                "model": "default-chat",
                "litellm_params": {"model": "openai/default-chat", "order": 2},
                "model_info": {"route_key": route_key},
            },
        )

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_applies_to_the_selected_deployment(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployment = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://api.example.test/v1",
                "order": 1,
            },
            "model_info": {
                "id": "dual-protocol-route",
                "provider": "primary",
                "api_key_name": "default",
                "upstream_url_surface": "openai/responses",
            },
        }
        error = RuntimeError("temporary responses upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(
            error,
            {
                "call_type": "aresponses",
                "model": "default-chat",
                "stream": True,
                "_young_router_upstream_url_surface": "openai/responses",
                "litellm_params": deployment["litellm_params"],
                "model_info": deployment["model_info"],
            },
        )

        responses_filtered = await hook.async_filter_deployments(
            "default-chat",
            [deployment],
            messages=None,
            request_kwargs={
                "call_type": "aresponses",
                "model": "default-chat",
                "stream": True,
            },
        )
        chat_filtered = await hook.async_filter_deployments(
            "default-chat",
            [deployment],
            messages=None,
            request_kwargs={
                "call_type": "aresponses",
                "model": "default-chat",
                "stream": True,
                "_young_router_upstream_url_surface": "openai/chat",
            },
        )

        self.assertEqual(responses_filtered, [])
        self.assertEqual(chat_filtered, [])
        self.assertEqual(
            hooks._request_surface_for_deployment({}, deployment),
            "openai/responses",
        )
        self.assertIn(
            "id:dual-protocol-route",
            hooks._DEPLOYMENT_COOLDOWNS,
        )
        self.assertFalse(any("|surface:" in key for key in hooks._DEPLOYMENT_COOLDOWNS))

    def test_surface_adapter_uses_exact_model_for_all_three_protocols(self) -> None:
        hooks, _ = load_hook_module()

        self.assertEqual(
            hooks._surface_adapter_model("openai/vendor/model", "openai/responses"),
            "openai/vendor/model",
        )
        self.assertEqual(
            hooks._surface_adapter_model("chatgpt/gpt-5.4", "openai/responses"),
            "chatgpt/gpt-5.4",
        )
        request = {"litellm_params": {"model": "chatgpt/gpt-5.4"}}
        hooks._apply_surface_adapter_to_request(
            request, "openai/responses", "chatgpt/gpt-5.4"
        )
        self.assertEqual(request["custom_llm_provider"], "chatgpt")
        self.assertEqual(request["litellm_params"]["model"], "chatgpt/gpt-5.4")

    def test_surface_adapter_relaxed_choice_removes_nested_forcing_copies(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "model": "openai/vendor/model",
            "tool_choice": {"type": "function", "name": "inspect"},
            "extra_body": {
                "tool_choice": {"type": "function", "name": "inspect"},
                "function_call": {"name": "inspect"},
                "keep": True,
            },
            "litellm_params": {
                "tool_choice": {"type": "function", "name": "inspect"},
                "function_call": {"name": "inspect"},
                "keep": True,
            },
            hooks._PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY: True,
        }

        hooks._apply_surface_adapter_to_request(
            request,
            "openai/chat",
            "openai/vendor/model",
        )

        self.assertEqual(request["tool_choice"], "auto")
        for key in ("extra_body", "litellm_params"):
            self.assertNotIn("tool_choice", request[key])
            self.assertNotIn("function_call", request[key])
            self.assertTrue(request[key]["keep"])

    def test_fallback_mode_starts_with_the_client_protocol(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "openai/kimi-k3", "order": 1},
            "model_info": {
                "id": "kimi-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }

        self.assertEqual(
            hooks._request_surface_for_deployment(
                {"call_type": "aresponses", "input": "hello"}, deployment
            ),
            "openai/responses",
        )
        self.assertEqual(
            hooks._request_surface_for_deployment(
                {"call_type": "messages", "messages": [{"role": "user"}]},
                deployment,
            ),
            "anthropic",
        )

    def test_fallback_chat_route_uses_chat_when_the_client_wants_a_summary(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "openai/deepseek-flash", "order": 1},
            "model_info": {
                "id": "chat-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
                # The route states that its Responses wire does not carry the
                # thinking this client asked to read.
                "supports_responses_reasoning_summary": False,
            },
        }

        self.assertEqual(
            hooks._request_surface_for_deployment(
                {
                    "call_type": "aresponses",
                    "input": "hello",
                    "reasoning": {"effort": "max", "summary": "auto"},
                },
                deployment,
            ),
            "openai/chat",
        )

    def test_fallback_chat_route_keeps_the_client_protocol_when_it_states_nothing(
        self,
    ) -> None:
        """A guessed chat backup must not replace the client's own protocol.

        ``upstream_url_surface: openai/chat`` on a ``fallback`` route is this
        app's inference for an unknown relay, not a statement about its
        ``/v1/responses`` wire.  Switching every summary-asking Responses turn
        onto Chat therefore moved a protocol the user never chose -- measured
        live 2026-11-05, a ``kimi-k3.1-pro`` relay's Responses wire was never
        tried while the log showed every turn leaving on Chat.
        """

        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "openai/kimi-k3.1-pro", "order": 0.3},
            "model_info": {
                "id": "relay-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }

        self.assertEqual(
            hooks._request_surface_for_deployment(
                {
                    "call_type": "aresponses",
                    "input": "hello",
                    "reasoning": {"effort": "max", "summary": "auto"},
                },
                deployment,
            ),
            "openai/responses",
        )

    def test_a_route_that_serves_responses_keeps_it(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "openai/vendor-model", "order": 1},
            "model_info": {
                "id": "declared-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
                "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
            },
        }

        self.assertEqual(
            hooks._request_surface_for_deployment(
                {
                    "call_type": "aresponses",
                    "input": "hello",
                    "reasoning": {"effort": "max", "summary": "auto"},
                },
                deployment,
            ),
            "openai/responses",
        )

    def test_fallback_chat_route_keeps_the_client_protocol_without_a_summary(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "openai/deepseek-flash", "order": 1},
            "model_info": {
                "id": "chat-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }

        self.assertEqual(
            hooks._request_surface_for_deployment(
                {
                    "call_type": "aresponses",
                    "input": "hello",
                    "reasoning": {"effort": "max", "summary": "none"},
                },
                deployment,
            ),
            "openai/responses",
        )

    def test_fixed_responses_route_keeps_its_surface_with_a_summary(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "openai/vendor-model", "order": 1},
            "model_info": {
                "id": "fixed-responses-route",
                "upstream_url_surface": "openai/responses",
                "upstream_protocol_mode": "fixed",
            },
        }

        self.assertEqual(
            hooks._request_surface_for_deployment(
                {
                    "call_type": "aresponses",
                    "input": "hello",
                    "reasoning": {"effort": "max", "summary": "auto"},
                },
                deployment,
            ),
            "openai/responses",
        )

    def test_fallback_responses_route_keeps_its_surface_with_a_summary(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "openai/vendor-model", "order": 1},
            "model_info": {
                "id": "responses-route",
                "upstream_url_surface": "openai/responses",
                "upstream_protocol_mode": "fallback",
            },
        }

        self.assertEqual(
            hooks._request_surface_for_deployment(
                {
                    "call_type": "aresponses",
                    "input": "hello",
                    "reasoning": {"effort": "max", "summary": "auto"},
                },
                deployment,
            ),
            "openai/responses",
        )

    def test_excluded_deployments_survive_the_litellm_metadata_rebuild(self) -> None:
        hooks, _ = load_hook_module()

        class Refusal(Exception):
            def __init__(self) -> None:
                super().__init__("refused")
                self.failed_deployment_id = "route-a"

        request = {"litellm_metadata": {}}
        hooks._sync_failed_deployment_exclusions(request, Refusal())

        self.assertEqual(["route-a"], request["_excluded_deployment_ids"])
        self.assertEqual(
            ["route-a"],
            request["litellm_metadata"]["excluded_deployment_ids"],
        )
        rebuilt = {"litellm_metadata": {"excluded_deployment_ids": ["route-a"]}}
        self.assertEqual(
            {"route-a"},
            hooks._request_excluded_deployment_ids(rebuilt),
        )

    def test_fixed_mode_ignores_a_stale_fallback_target(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "openai/kimi-k3", "order": 1},
            "model_info": {
                "id": "fixed-route",
                "upstream_url_surface": "anthropic",
                "upstream_protocol_mode": "fixed",
            },
        }

        request = {
            "call_type": "aresponses",
            "input": "hello",
            "_young_router_upstream_url_surface": "openai/chat",
            "_young_router_upstream_url_surface_deployment_id": "fixed-route",
            "_young_router_surface_target_deployment_id": "fixed-route",
        }

        self.assertEqual(
            hooks._request_surface_for_deployment(request, deployment),
            "anthropic",
        )

    def test_structured_compaction_respects_fixed_route_surface(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "anthropic/vendor-model", "order": 1},
            "model_info": {
                "id": "fixed-anthropic-route",
                "upstream_url_surface": "anthropic",
                "upstream_protocol_mode": "fixed",
            },
        }
        request = {
            "call_type": "aresponses",
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "_young_router_upstream_url_surface": "anthropic",
            "_young_router_upstream_url_surface_deployment_id": "fixed-anthropic-route",
        }

        self.assertEqual(
            hooks._request_surface_for_deployment(request, deployment),
            "anthropic",
        )

    def test_structured_compaction_allows_configured_same_route_fallback(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "openai/vendor-model", "order": 1},
            "model_info": {
                "id": "dual-protocol-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [deployment]

        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "stream": True,
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "_target_order": 1,
            "_young_router_upstream_url_surface": "openai/responses",
            "_young_router_attempted_upstream_url_surfaces": ["openai/responses"],
            "_young_router_upstream_url_surface_deployment_id": "dual-protocol-route",
            "model_info": deployment["model_info"],
            "litellm_params": deployment["litellm_params"],
        }
        error = RuntimeError("Responses endpoint not found")
        error.status_code = 404
        hooks._mark_exception_for_upstream_surface_failover(error, request)

        entry = hooks._next_upstream_surface_for_failed_deployment(
            Router(), error, request
        )

        self.assertEqual(entry, ("openai/chat", "dual-protocol-route"))

    def test_structured_compaction_skips_fixed_non_responses_route(self) -> None:
        hooks, _ = load_hook_module()
        primary = {
            "litellm_params": {"model": "anthropic/vendor-model", "order": 1},
            "model_info": {
                "id": "anthropic-route",
                "upstream_url_surface": "anthropic",
                "upstream_protocol_mode": "fixed",
            },
        }
        peer = {
            "litellm_params": {"model": "openai/vendor-model", "order": 1},
            "model_info": {
                "id": "responses-peer",
                "upstream_url_surface": "openai/responses",
                "upstream_protocol_mode": "fallback",
            },
        }

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [primary, peer]

        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "stream": True,
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "_target_order": 1,
            "_young_router_upstream_url_surface": "openai/responses",
            "_young_router_attempted_upstream_url_surfaces": ["openai/responses"],
            "_young_router_upstream_url_surface_deployment_id": "anthropic-route",
            "model_info": primary["model_info"],
            "litellm_params": primary["litellm_params"],
        }
        error = RuntimeError("Responses endpoint not found")
        error.status_code = 404
        hooks._mark_exception_for_upstream_surface_failover(error, request)

        entry = hooks._ordered_deployment_fallback_entry(Router(), error, request)

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["_target_order"], 1)
        self.assertEqual(entry["_excluded_deployment_ids"], ["anthropic-route"])
        self.assertEqual(
            entry[hooks._VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY],
            ["responses-peer"],
        )
        self.assertNotIn("_young_router_upstream_url_surface", entry)

    def test_protocol_fallback_stays_on_the_same_deployment(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployment = {
            "litellm_params": {"model": "openai/kimi-k3", "order": 1},
            "model_info": {
                "id": "kimi-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [deployment]

        request = {
            "model": "default-chat",
            "call_type": "messages",
            "messages": [{"role": "user", "content": "hello"}],
            "_target_order": 1,
            "_young_router_upstream_url_surface": "anthropic",
            "_young_router_attempted_upstream_url_surfaces": ["anthropic"],
            "_young_router_upstream_url_surface_deployment_id": "kimi-route",
            "model_info": deployment["model_info"],
            "litellm_params": deployment["litellm_params"],
        }
        error = RuntimeError("messages endpoint not found")
        error.status_code = 404
        hooks._mark_exception_for_upstream_surface_failover(error, request)

        entry = hooks._ordered_deployment_fallback_entry(Router(), error, request)

        self.assertIsNotNone(entry)
        self.assertEqual(entry["_target_order"], 1)
        self.assertEqual(entry["_young_router_upstream_url_surface"], "openai/chat")
        self.assertEqual(
            entry["_young_router_surface_target_deployment_id"], "kimi-route"
        )
        self.assertEqual(
            request["_young_router_protocol_fallback_from_surface"], "anthropic"
        )
        self.assertFalse(hooks._DEPLOYMENT_COOLDOWNS)

    def test_invalid_parameter_combination_uses_protocol_fallback(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        deployment = {
            "litellm_params": {"model": "openai/vendor-model", "order": 0},
            "model_info": {
                "id": "dual-protocol-route",
                "order": 0,
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [deployment]

        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "input": "hello",
            "stream": True,
            "_young_router_upstream_url_surface": "openai/responses",
            "_young_router_attempted_upstream_url_surfaces": [
                "openai/responses"
            ],
            "_young_router_upstream_url_surface_deployment_id": (
                "dual-protocol-route"
            ),
            "model_info": deployment["model_info"],
            "litellm_params": deployment["litellm_params"],
        }
        error = RuntimeError(
            'OpenAIException - {"error":{"code":"server_error",'
            '"message":"请求参数组合无效"}}'
        )
        error.status_code = 400

        self.assertTrue(
            hooks._is_current_upstream_surface_incompatible_error(
                error,
                request,
            )
        )
        hooks._mark_exception_for_upstream_surface_failover(error, request)
        entry = hooks._ordered_deployment_fallback_entry(
            Router(),
            error,
            request,
        )

        self.assertIsNotNone(entry)
        self.assertEqual(
            entry["_young_router_upstream_url_surface"],
            "openai/chat",
        )
        self.assertEqual(
            entry["_young_router_surface_target_deployment_id"],
            "dual-protocol-route",
        )
        self.assertFalse(hooks._DEPLOYMENT_COOLDOWNS)

    def test_failed_protocol_fallback_counts_once_without_recovery(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        with tempfile.TemporaryDirectory() as directory:
            self.set_env(
                hooks._DEPLOYMENT_COOLDOWN_FILE_ENV,
                str(Path(directory) / "deployment-cooldowns.json"),
            )
            request = {
                "model": "default-chat",
                "call_type": "aresponses",
                "input": "hello",
                "stream": True,
                "_young_router_upstream_url_surface": "openai/chat",
                "_young_router_attempted_upstream_url_surfaces": [
                    "openai/responses",
                    "openai/chat",
                ],
                "_young_router_upstream_url_surface_deployment_id": (
                    "dual-protocol-route"
                ),
                "_young_router_protocol_fallback_from_surface": (
                    "openai/responses"
                ),
                "_young_router_protocol_fallback_client_surface": (
                    "openai/responses"
                ),
                "model_info": {
                    "id": "dual-protocol-route",
                    "order": 0,
                    "upstream_url_surface": "openai/chat",
                    "upstream_protocol_mode": "fallback",
                },
                "litellm_params": {
                    "model": "openai/vendor-model",
                    "order": 0,
                },
            }
            error = RuntimeError("fallback request rejected")
            error.status_code = 400

            hooks._mark_exception_for_deployment_failover(error, request)
            hooks._mark_exception_for_deployment_failover(error, request)

            self.assertNotIn("id:dual-protocol-route", hooks._DEPLOYMENT_COOLDOWNS)
            self.assertEqual(
                hooks._recovery_policy_for_exception(error),
                hooks._RECOVERY_POLICY_ERROR,
            )
            self.assertFalse(hooks._is_route_recovery_poll_error(error))
            self.assertFalse(
                hooks._protocol_fallback_attempt_active(request)
            )

    def test_protocol_fallback_request_error_cooldown_requires_explicit_policy(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(
            hooks._RECOVERY_POLICY_REQUEST_ERROR_ENV,
            hooks._RECOVERY_POLICY_COOLDOWN,
        )
        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "input": "hello",
            "stream": True,
            "_young_router_upstream_url_surface": "openai/chat",
            "_young_router_protocol_fallback_from_surface": "openai/responses",
            "_young_router_protocol_fallback_client_surface": "openai/responses",
            "_young_router_upstream_url_surface_deployment_id": "dual-protocol-route",
            "model_info": {
                "id": "dual-protocol-route",
                "order": 0,
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
            "litellm_params": {"model": "openai/vendor-model", "order": 0},
        }
        error = RuntimeError("fallback request rejected")
        error.status_code = 400

        hooks._mark_exception_for_deployment_failover(error, request)

        state = hooks._DEPLOYMENT_COOLDOWNS["id:dual-protocol-route"]
        self.assertEqual(state["failures"], 1)
        self.assertGreater(state.get("cooldown_until", 0), 0)
        self.assertEqual(
            hooks._recovery_policy_for_exception(error),
            hooks._RECOVERY_POLICY_COOLDOWN,
        )

    def test_protocol_fallback_wrapper_failure_does_not_count_cooldown_twice(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "input": "hello",
            "stream": True,
            "_young_router_upstream_url_surface": "openai/chat",
            "_young_router_upstream_url_surface_deployment_id": "dual-protocol-route",
            "_young_router_protocol_fallback_from_surface": "openai/responses",
            "_young_router_protocol_fallback_client_surface": "openai/responses",
            "model_info": {
                "id": "dual-protocol-route",
                "order": 0,
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
            "litellm_params": {
                "model": "openai/vendor-model",
                "order": 0,
            },
        }
        first = RuntimeError("responses protocol failed")
        first.status_code = 400
        hooks._mark_exception_for_deployment_failover(first, request)

        wrapper = RuntimeError("chat wrapper failed")
        wrapper.status_code = 503
        hooks._mark_exception_for_deployment_failover(wrapper, request)

        state = hooks._DEPLOYMENT_COOLDOWNS["id:dual-protocol-route"]
        self.assertEqual(state["failures"], 1)
        self.assertEqual(state.get("cooldown_until", 0), 0)

    def test_protocol_fallback_failure_marker_does_not_hide_a_different_route(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        request = {
            "model": "default-chat",
            "model_info": {
                "id": "route-a",
                "order": 0,
                "upstream_url_surface": "openai/chat",
            },
            "litellm_params": {"model": "openai/vendor-model"},
            "_young_router_upstream_url_surface": "openai/chat",
            "_young_router_protocol_fallback_from_surface": "openai/responses",
        }
        first = RuntimeError("protocol chain failed")
        first.status_code = 400
        hooks._mark_exception_for_deployment_failover(first, request)

        request["model_info"] = {
            "id": "route-b",
            "order": 1,
            "upstream_url_surface": "openai/chat",
        }
        request["litellm_params"] = {"model": "openai/other-model"}
        wrapper = RuntimeError("different route failed")
        wrapper.status_code = 503
        hooks._mark_exception_for_deployment_failover(wrapper, request)

        self.assertNotIn("id:route-a", hooks._DEPLOYMENT_COOLDOWNS)
        self.assertEqual(hooks._DEPLOYMENT_COOLDOWNS["id:route-b"]["failures"], 1)

    def test_successful_protocol_fallback_is_remembered_for_runtime_ttl(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployment = {
            "litellm_params": {"model": "openai/kimi-k3", "order": 1},
            "model_info": {
                "id": "kimi-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            self.set_env(hooks._DEPLOYMENT_COOLDOWN_FILE_ENV, str(Path(directory) / "routing.json"))
            self.set_env(hooks._PROTOCOL_FALLBACK_TTL_SECONDS_ENV, "600")
            failed_request = {
                "model": "default-chat",
                "_young_router_upstream_url_surface": "anthropic",
                "_young_router_upstream_url_surface_deployment_id": "kimi-route",
                "model_info": deployment["model_info"],
                "litellm_params": deployment["litellm_params"],
            }
            initial_error = RuntimeError("initial protocol rejected")
            initial_error.status_code = 503
            hooks._mark_exception_for_deployment_failover(
                initial_error,
                failed_request,
            )
            self.assertIn("id:kimi-route", hooks._DEPLOYMENT_COOLDOWNS)

            request = {
                "model": "default-chat",
                "call_type": "messages",
                "messages": [{"role": "user", "content": "hello"}],
                "_young_router_upstream_url_surface": "openai/chat",
                "_young_router_upstream_url_surface_deployment_id": "kimi-route",
                "_young_router_protocol_fallback_from_surface": "anthropic",
                "_young_router_protocol_fallback_client_surface": "anthropic",
                "model_info": deployment["model_info"],
            }
            hooks._record_protocol_fallback_success(request)
            self.assertNotIn("id:kimi-route", hooks._DEPLOYMENT_COOLDOWNS)

            next_request = {
                "call_type": "messages",
                "messages": [{"role": "user", "content": "again"}],
            }
            self.assertEqual(
                hooks._request_surface_for_deployment(next_request, deployment),
                "openai/chat",
            )

    def test_invalid_protocol_fallback_advances_to_the_next_order(self) -> None:
        hooks, _ = load_hook_module()
        primary = {
            "litellm_params": {"model": "openai/kimi-k3", "order": 1},
            "model_info": {
                "id": "kimi-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }
        secondary = {
            "litellm_params": {"model": "openai/gpt-5", "order": 2},
            "model_info": {
                "id": "gpt-route",
                "upstream_url_surface": "openai/responses",
                "upstream_protocol_mode": "fallback",
            },
        }

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [primary, secondary]

        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "input": "hello",
            "_target_order": 1,
            "_young_router_upstream_url_surface": "openai/chat",
            "_young_router_attempted_upstream_url_surfaces": [
                "openai/responses",
                "openai/chat",
            ],
            "_young_router_upstream_url_surface_deployment_id": "kimi-route",
            "model_info": primary["model_info"],
            "litellm_params": primary["litellm_params"],
        }
        error = RuntimeError("chat endpoint not found")
        error.status_code = 404
        hooks._mark_exception_for_upstream_surface_failover(error, request)

        entry = hooks._ordered_deployment_fallback_entry(Router(), error, request)

        self.assertIsNotNone(entry)
        self.assertEqual(entry["_target_order"], 2)
        self.assertEqual(entry["_excluded_deployment_ids"], ["kimi-route"])
        self.assertNotIn("_young_router_upstream_url_surface", entry)
        self.assertEqual(
            hooks._surface_adapter_model("anthropic/vendor/model", "openai/chat"),
            "openai/vendor/model",
        )

    def test_protocol_incompatibility_skips_to_a_same_order_peer(self) -> None:
        hooks, _ = load_hook_module()
        deployment_a = {
            "litellm_params": {"model": "openai/vendor-a", "order": 1},
            "model_info": {
                "id": "route-a",
                "upstream_url_surface": "openai/responses",
            },
        }
        deployment_b = {
            "litellm_params": {"model": "openai/vendor-b", "order": 1},
            "model_info": {
                "id": "route-b",
                "upstream_url_surface": "openai/chat",
            },
        }

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [deployment_a, deployment_b]

        request = {
            "model": "default-chat",
            "_target_order": 1,
            "_young_router_upstream_url_surface": "openai/responses",
            "model_info": deployment_a["model_info"],
            "litellm_params": deployment_a["litellm_params"],
        }

        first_error = RuntimeError("responses endpoint not found")
        first_error.status_code = 404
        hooks._mark_exception_for_deployment_failover(first_error, request)
        first = hooks._ordered_deployment_fallback_entry(Router(), first_error, request)

        self.assertEqual(first["_target_order"], 1)
        self.assertEqual(first["_excluded_deployment_ids"], ["route-a"])
        self.assertEqual(
            first["_young_router_verified_fallback_deployment_ids"],
            ["route-b"],
        )
        self.assertNotIn("_young_router_upstream_url_surface", first)
        self.assertFalse(any("surface_target" in key for key in first))

    async def test_deployment_is_filtered_after_its_selected_protocol_fails(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployment = {
            "litellm_params": {"model": "openai/vendor-a", "order": 1},
            "model_info": {
                "id": "three-surface-route",
                "upstream_url_surface": "anthropic",
            },
        }

        error = RuntimeError("temporary anthropic failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(
            error,
            {
                "model": "default-chat",
                "_young_router_upstream_url_surface": "anthropic",
                "litellm_params": deployment["litellm_params"],
                "model_info": deployment["model_info"],
            },
        )
        filtered = await hook.async_filter_deployments(
            "default-chat",
            [deployment],
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [])

    def test_current_surface_incompatibility_is_narrowly_classified(self) -> None:
        hooks, _ = load_hook_module()
        request = {"_young_router_upstream_url_surface": "openai/responses"}
        endpoint_error = RuntimeError("endpoint not found")
        endpoint_error.status_code = 404
        schema_error = RuntimeError(
            "Invalid Responses API request: invalid_union; expected string, received array"
        )
        schema_error.status_code = 400
        policy_error = RuntimeError("request violates content policy")
        policy_error.status_code = 400

        self.assertTrue(
            hooks._is_current_upstream_surface_incompatible_error(endpoint_error, request)
        )
        self.assertTrue(
            hooks._is_current_upstream_surface_incompatible_error(schema_error, request)
        )
        self.assertFalse(
            hooks._is_current_upstream_surface_incompatible_error(policy_error, request)
        )

    def test_forced_tool_choice_rejection_uses_configured_protocol_fallback(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "tool_choice": {"type": "function", "name": "inspect"},
            "_young_router_upstream_url_surface": "openai/responses",
            "model_info": {
                "id": "dual-protocol-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }
        error = RuntimeError(
            "当前模型或上游不支持指定工具的强制选择方式，请改用 tool_choice=auto"
        )
        error.status_code = 400

        self.assertTrue(
            hooks._is_forced_tool_choice_unsupported_error(error, request)
        )
        self.assertTrue(
            hooks._is_current_upstream_surface_incompatible_error(error, request)
        )

        auto_request = {**request, "tool_choice": "auto"}
        self.assertFalse(
            hooks._is_forced_tool_choice_unsupported_error(error, auto_request)
        )
        self.assertFalse(
            hooks._is_current_upstream_surface_incompatible_error(error, auto_request)
        )

        no_choice_request = {
            key: value for key, value in request.items() if key != "tool_choice"
        }
        self.assertFalse(
            hooks._is_forced_tool_choice_unsupported_error(error, no_choice_request)
        )

    def test_a_thinking_route_refusal_reaches_the_auto_retry(self) -> None:
        # The official DeepSeek endpoint answers this wording to a payload that
        # carries ``tool_choice: required`` while thinking, and serves the same
        # payload with ``auto`` (measured live 2026-11-04).  The wording names the
        # mode, never "forced", so a matcher that required the forced wording
        # left every turn paying the 400 before a sibling answered.
        hooks, _ = load_hook_module()
        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "tool_choice": "required",
            "tools": [{"type": "function", "name": "inspect"}],
            "_young_router_upstream_url_surface": "openai/chat",
            "model_info": {
                "id": "thinking-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }
        error = RuntimeError(
            "litellm.BadRequestError: OpenAIException - Thinking mode does not "
            "support this tool_choice (request_id: 4b4fb7bb-e1ee-4f68-ad87)"
        )
        error.status_code = 400

        self.assertTrue(
            hooks._is_forced_tool_choice_unsupported_error(error, request)
        )
        self.assertTrue(
            hooks._is_forced_tool_choice_auto_retry_error(error, request)
        )

        auto_request = {**request, "tool_choice": "auto"}
        self.assertFalse(
            hooks._is_forced_tool_choice_unsupported_error(error, auto_request)
        )
        self.assertFalse(
            hooks._is_forced_tool_choice_auto_retry_error(error, auto_request)
        )

    def test_ambiguous_forced_choice_retry_requires_a_tool_payload(self) -> None:
        hooks, _ = load_hook_module()
        error = RuntimeError("请求参数组合无效")
        error.status_code = 400
        request = {
            "tool_choice": {"type": "function", "name": "inspect"},
        }

        self.assertFalse(
            hooks._is_forced_tool_choice_auto_retry_error(error, request)
        )

        request["tools"] = [{"type": "function", "name": "inspect"}]
        self.assertTrue(
            hooks._is_forced_tool_choice_auto_retry_error(error, request)
        )

    def test_route_key_canonicalizes_api_base_host(self) -> None:
        hooks, _ = load_hook_module()
        old_route_key = "compat_provider / openai/default-chat / key=x-plus / order=2"
        backup_provider = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://api.backup.example/v1",
                "order": 2,
            },
            "model_info": {
                "id": "backup_provider-a",
                "provider": "compat_provider",
                "api_key_name": "x-plus",
                "route_key": old_route_key,
            },
        }
        compat_provider = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://headers.example/v1",
                "order": 2,
            },
            "model_info": {
                "id": "compat_provider-a",
                "provider": "compat_provider",
                "api_key_name": "x-plus",
                "route_key": old_route_key,
            },
        }

        backup_provider_key = hooks._deployment_route_key_from_deployment(backup_provider)
        compat_provider_key = hooks._deployment_route_key_from_deployment(compat_provider)

        self.assertEqual(
            backup_provider_key,
            "provider=compat_provider / upstream=openai/default-chat / host=api.backup.example / key=x-plus / order=2",
        )
        self.assertEqual(
            compat_provider_key,
            "provider=compat_provider / upstream=openai/default-chat / host=headers.example / key=x-plus / order=2",
        )
        self.assertNotEqual(backup_provider_key, compat_provider_key)
        self.assertEqual(
            hooks._deployment_route_key_from_request(
                {
                    "litellm_params": backup_provider["litellm_params"],
                    "model_info": backup_provider["model_info"],
                }
            ),
            backup_provider_key,
        )

    def test_route_key_includes_public_model_group_when_available(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "model_name": "llmwebsearch",
            "litellm_params": {
                "model": "openai/vendor/vendor-chat",
                "api_base": "https://openrouter.ai/api/v1",
                "order": 1,
            },
            "model_info": {
                "id": "openrouter-chat",
                "provider": "openrouter",
                "api_key_name": "default",
            },
        }

        self.assertEqual(
            hooks._deployment_route_key_from_deployment(deployment),
            "model=llmwebsearch / provider=openrouter / upstream=openai/vendor/vendor-chat / host=openrouter.ai / key=default / order=1",
        )
        self.assertEqual(
            hooks._deployment_route_key_from_request(
                {
                    "model": "llmwebsearch",
                    "litellm_params": deployment["litellm_params"],
                    "model_info": deployment["model_info"],
                }
            ),
            "model=llmwebsearch / provider=openrouter / upstream=openai/vendor/vendor-chat / host=openrouter.ai / key=default / order=1",
        )

    async def test_deployment_cooldown_deployment_id_does_not_cross_api_base_hosts(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        old_route_key = "compat_provider / openai/default-chat / key=x-plus / order=2"
        deployments = [
            {
                "litellm_params": {
                    "model": "openai/default-chat",
                    "api_base": "https://api.backup.example/v1",
                    "order": 2,
                },
                "model_info": {
                    "id": "new-backup_provider",
                    "provider": "compat_provider",
                    "api_key_name": "x-plus",
                    "route_key": old_route_key,
                },
            },
            {
                "litellm_params": {
                    "model": "openai/default-chat",
                    "api_base": "https://headers.example/v1",
                    "order": 2,
                },
                "model_info": {
                    "id": "new-compat_provider",
                    "provider": "compat_provider",
                    "api_key_name": "x-plus",
                    "route_key": old_route_key,
                },
            },
        ]
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503

        hooks._mark_exception_for_deployment_failover(
            error,
            {
                "model": "default-chat",
                "litellm_params": {
                    "model": "openai/default-chat",
                    "api_base": "https://api.backup.example/v1",
                    "order": 2,
                },
                "model_info": {
                    "id": "old-backup_provider",
                    "provider": "compat_provider",
                    "api_key_name": "x-plus",
                    "route_key": old_route_key,
                },
            },
        )

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )

        self.assertEqual(filtered, deployments)

    async def test_responses_api_does_not_apply_order_before_router(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {
                    "api_base": "https://api.primary.example/v1",
                    "order": 1,
                },
                "model_info": {"id": "primary_provider", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {
                    "api_base": "https://headers.example/v1",
                    "order": 3,
                },
                "model_info": {"id": "compat_provider-pro", "supports_responses_image_generation_tool": True},
            },
            {
                "litellm_params": {
                    "api_base": "https://headers.example/v1",
                    "order": 2,
                },
                "model_info": {"id": "compat_provider-normal", "supports_responses_image_generation_tool": False},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={"original_generic_function": aresponses},
        )

        self.assertEqual(filtered, deployments)

    async def test_responses_api_fallback_target_order_is_honored(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {
                    "api_base": "https://headers.example/v1",
                    "order": 2,
                },
                "model_info": {"id": "compat_provider-normal", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {
                    "api_base": "https://api.backup.example/v1",
                    "order": 3,
                },
                "model_info": {"id": "backup_provider", "supports_responses_image_generation_tool": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "original_generic_function": aresponses,
                "_target_order": 3,
            },
        )

        self.assertEqual(filtered, [deployments[1]])

    async def test_filter_deployments_honors_weighted_failover_exclusions_before_preferences(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {
                    "api_base": "https://headers.example/v1",
                    "order": 2,
                },
                "model_info": {"id": "compat_provider-normal", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {
                    "api_base": "https://api.backup.example/v1",
                    "order": 3,
                },
                "model_info": {"id": "backup_provider", "supports_responses_image_generation_tool": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "original_generic_function": aresponses,
                "_excluded_deployment_ids": ["compat_provider-normal"],
            },
        )

        self.assertEqual(filtered, [deployments[1]])

    async def test_filter_deployments_ignores_prompt_without_structured_tool(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        deployments = [
            {"model_info": {"supports_responses_image_generation_tool": False}},
            {"model_info": {"supports_responses_image_generation_tool": True}},
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=[{"role": "user", "content": "image_generation tool request please"}],
            request_kwargs={},
        )

        self.assertEqual(filtered, deployments)

    async def test_no_deployments_for_order_continues_to_next_order(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            exc = RuntimeError("upstream 503 on cheap")
            exc.status_code = 503
            raise exc

        async def pro_stream():
            yield {"type": "response.output_text.delta", "delta": "pro ok"}
            yield {"type": "response.completed", "response": {"id": "resp-pro"}}

        class RouterRateLimitError(Exception):
            pass

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "cheap-a"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "plus-a"},
                    },
                    {
                        "litellm_params": {"order": 3},
                        "model_info": {"id": "pro-a"},
                    },
                ]

            async def aresponses(self, **payload):
                calls.append(payload)
                if payload.get("_target_order") == 2:
                    raise RouterRateLimitError("No deployments available for selected model")
                if payload.get("_target_order") == 3:
                    return pro_stream()
                raise AssertionError(f"unexpected target order: {payload.get('_target_order')}")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Say hi."}],
            "stream": True,
            "model_info": {"id": "cheap-a", "order": 1},
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual([call.get("_target_order") for call in calls], [2, 3])
        # The turn opens once, under the router's id, before any order is asked;
        # the answer from the order that served it arrives inside that turn.
        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            [
                "response.created",
                "response.in_progress",
                "response.output_text.delta",
                "response.completed",
            ],
        )
        self.assertEqual(chunks[2]["delta"], "pro ok")
        announced_id = chunks[0]["response"]["id"]
        self.assertEqual(chunks[-1]["response"]["id"], announced_id)

    def test_route_recovery_next_poll_order_uses_unfiltered_configured_orders(self) -> None:
        hooks, _ = load_hook_module()
        deployments = [
            {
                "litellm_params": {"order": 2},
                "model_info": {"id": "plus-a"},
            },
            {
                "litellm_params": {"order": 3},
                "model_info": {"id": "pro-a"},
            },
        ]

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

        def original_get_all_deployments(self, model_name, team_id=None):
            return deployments

        FakeRouter._get_all_deployments._original_get_all_deployments = original_get_all_deployments
        exc = RuntimeError("No deployments available for selected model")
        exc.failed_deployment_order = 3
        request_data = {
            "model": "default-chat",
            "_excluded_deployment_ids": ["plus-a", "pro-a"],
        }
        token = hooks._CURRENT_EXCLUDED_DEPLOYMENT_IDS.set({"plus-a", "pro-a"})
        try:
            next_order = hooks._route_recovery_next_poll_order(
                FakeRouter(),
                request_data,
                exc,
            )
        finally:
            hooks._CURRENT_EXCLUDED_DEPLOYMENT_IDS.reset(token)

        self.assertEqual(next_order, 2)

    def test_ordered_deployment_fallback_uses_unfiltered_configured_deployments(self) -> None:
        hooks, _ = load_hook_module()
        deployments = [
            {
                "litellm_params": {"order": 2},
                "model_info": {"id": "plus-a"},
            },
            {
                "litellm_params": {"order": 3},
                "model_info": {"id": "pro-a"},
            },
        ]

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

        def original_get_all_deployments(self, model_name, team_id=None):
            return deployments

        FakeRouter._get_all_deployments._original_get_all_deployments = original_get_all_deployments
        exc = RuntimeError("upstream 502 on plus")
        exc.status_code = 502
        exc.failed_deployment_id = "plus-a"
        exc.failed_deployment_order = 2
        request_data = {
            "model": "default-chat",
            "_excluded_deployment_ids": ["plus-a"],
        }
        token = hooks._CURRENT_EXCLUDED_DEPLOYMENT_IDS.set({"plus-a", "pro-a"})
        try:
            fallback_entry = hooks._ordered_deployment_fallback_entry(
                FakeRouter(),
                exc,
                request_data,
            )
        finally:
            hooks._CURRENT_EXCLUDED_DEPLOYMENT_IDS.reset(token)

        self.assertEqual(
            fallback_entry,
            {
                "model": "default-chat",
                "_target_order": 3,
                "_excluded_deployment_ids": ["plus-a"],
                hooks._VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY: ["pro-a"],
            },
        )

    def test_route_recovery_ignores_compatible_bad_request(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "OpenAIException invalid_request_error: system messages are not allowed"
        )
        exc.status_code = 400
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        self.assertFalse(hooks._is_route_recovery_poll_error(exc))
        self.assertFalse(hooks._should_return_route_recovery_stream(exc, request_data))

    def test_image_parameter_bad_request_is_fallback_eligible(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "OpenAIException invalid_request_error: unsupported image size 1792x1024"
        )
        exc.status_code = 400

        self.assertTrue(hooks._is_image_parameter_or_capability_bad_request_error(exc))
        self.assertTrue(hooks._is_priority_deployment_failover_error(exc))
        self.assertFalse(hooks._should_sanitize_final_upstream_route_error(exc))

    def test_image_generation_tool_unsupported_422_is_fallback_eligible(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "invalid_request_error: unsupported tool type image_generation"
        )
        exc.status_code = 422

        self.assertTrue(hooks._is_image_parameter_or_capability_bad_request_error(exc))
        self.assertTrue(hooks._is_priority_deployment_failover_error(exc))

    def test_responses_schema_bad_request_is_not_deployment_failover(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            'OpenAIException - {"error":{"code":"invalid_prompt",'
            '"message":"Invalid Responses API request"},'
            '"metadata":{"raw":"[{\\n \\\"code\\\": \\\"invalid_union\\\",'
            '\\n \\\"errors\\\": [[{\\n \\\"expected\\\": \\\"string\\\",'
            '\\n \\\"code\\\": \\\"invalid_type\\\",'
            '\\n \\\"message\\\": \\\"Invalid input: expected string, received array\\\"'
            '}]]}]"}}'
        )
        exc.status_code = 400

        self.assertTrue(hooks._is_responses_schema_unsupported_error(exc))
        self.assertFalse(hooks._is_image_parameter_or_capability_bad_request_error(exc))
        self.assertFalse(hooks._is_deployment_compatible_bad_request_error(exc))
        self.assertFalse(hooks._is_priority_deployment_failover_error(exc))
        self.assertFalse(hooks._should_sanitize_final_upstream_route_error(exc))
        self.assertEqual(
            hooks._trace_exception(exc)["reason"],
            "responses-schema-unsupported",
        )

    def test_ssl_verification_error_is_fallback_eligible(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "APIConnectionError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self signed certificate"
        )

        self.assertTrue(hooks._is_ssl_verification_error(exc))
        self.assertFalse(hooks._exception_indicates_network_connectivity_error(exc))
        self.assertTrue(hooks._is_priority_deployment_failover_error(exc))
        self.assertFalse(hooks._should_sanitize_final_upstream_route_error(exc))
        self.assertFalse(hooks._should_retry_same_deployment_before_fallback(exc))

    def test_image_generation_tool_runtime_fallback_uses_remaining_route_once(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {"model_info": {"id": "route-a", "order": 1}},
                    {"model_info": {"id": "route-b", "order": 1}},
                ]

        exc = RuntimeError("invalid_request_error: unsupported tool type image_generation")
        exc.status_code = 422
        exc.failed_deployment_id = "route-a"
        exc.failed_deployment_order = 1
        request_kwargs = {
            "model": "default-chat",
            "tools": [{"type": "image_generation"}],
            "litellm_metadata": {
                hooks._IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY: 3,
            },
        }

        entry = hooks._ordered_deployment_fallback_entry(FakeRouter(), exc, request_kwargs)

        self.assertEqual(
            entry,
            {
                "model": "default-chat",
                "_target_order": 1,
                "_excluded_deployment_ids": ["route-a"],
                hooks._VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY: ["route-b"],
            },
        )

    async def test_image_tool_capability_rejection_is_cached_across_workers(self) -> None:
        hooks, _proxy_server = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED_TTL_SECONDS_ENV, "600")
        hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED.clear()
        self.addCleanup(hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED.clear)
        deployments = [
            {"model_info": {"id": "image-route-a", "order": 1}},
            {"model_info": {"id": "image-route-b", "order": 2}},
        ]
        request_data = {
            "model": "default-chat",
            "tools": [{"type": "image_generation"}],
            "model_info": {"id": "image-route-a", "order": 1},
        }
        error = RuntimeError("invalid_request_error: unsupported tool type image_generation")
        error.status_code = 422

        with tempfile.TemporaryDirectory() as temp_dir:
            self.set_env(
                hooks._DEPLOYMENT_COOLDOWN_FILE_ENV,
                str(Path(temp_dir) / "deployment-cooldowns.json"),
            )
            hooks._mark_exception_for_deployment_failover(error, request_data)
            self.assertIn("id:image-route-a", hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED)
            hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED.clear()

            filtered = await hook.async_filter_deployments(
                "default-chat",
                deployments,
                messages=None,
                request_kwargs={
                    "model": "default-chat",
                    "tools": [{"type": "image_generation"}],
                },
            )

        self.assertEqual(filtered, [deployments[1]])

    def test_image_tool_memory_ignores_parameter_and_transient_errors(self) -> None:
        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED_TTL_SECONDS_ENV, "600")
        hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED.clear()
        self.addCleanup(hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED.clear)
        request_data = {
            "model": "default-chat",
            "tools": [{"type": "image_generation"}],
            "model_info": {"id": "image-route-a", "order": 1},
        }
        parameter_error = RuntimeError(
            "invalid_request_error: unsupported image size 1792x1024"
        )
        parameter_error.status_code = 400
        timeout_error = RuntimeError("gateway timeout while generating image")
        timeout_error.status_code = 504

        self.assertFalse(
            hooks._is_image_generation_tool_capability_error(parameter_error)
        )
        self.assertFalse(
            hooks._is_image_generation_tool_capability_error(timeout_error)
        )
        hooks._mark_exception_for_deployment_failover(parameter_error, request_data)
        hooks._mark_exception_for_deployment_failover(timeout_error, request_data)

        self.assertEqual(hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED, {})

    def test_all_image_tool_routes_unsupported_returns_terminal_client_error(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {"model_info": {"id": "image-route-a", "order": 1}},
                    {"model_info": {"id": "image-route-b", "order": 2}},
                ]

        error = RuntimeError("invalid_request_error: unsupported tool type image_generation")
        error.status_code = 422
        error.failed_deployment_id = "image-route-b"
        error.failed_deployment_order = 2
        request_data = {
            "model": "default-chat",
            "stream": True,
            "tools": [{"type": "image_generation"}],
            "litellm_metadata": {
                hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED_METADATA_KEY: [
                    "id:image-route-a",
                    "id:image-route-b",
                ]
            },
        }

        entry = hooks._ordered_deployment_fallback_entry(
            FakeRouter(),
            error,
            request_data,
        )
        event = hooks._synthesized_failed_response_event(request_data, error)

        self.assertIsNone(entry)
        self.assertTrue(
            hooks._is_image_generation_all_deployments_unsupported_error(error)
        )
        self.assertEqual(event["response"]["error"]["type"], "invalid_request_error")
        self.assertEqual(
            event["response"]["error"]["code"],
            "image_generation_tool_unavailable",
        )

    def test_fallback_stops_after_each_image_tool_route_rejected_it(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "model_info": {
                            "id": "route-a",
                            "order": 1,
                            "provider": "provider-a",
                            "api_key_name": "key-a",
                        },
                        "litellm_params": {
                            "model": "openai/route-a",
                            "api_base": "https://route-a.example/v1",
                        },
                    },
                    {
                        "model_info": {
                            "id": "route-b",
                            "order": 2,
                            "provider": "provider-b",
                            "api_key_name": "key-b",
                        },
                        "litellm_params": {
                            "model": "openai/route-b",
                            "api_base": "https://route-b.example/v1",
                        },
                    },
                ]

        error = RuntimeError(
            "invalid_request_error: unsupported tool type image_generation"
        )
        error.status_code = 422
        error.failed_deployment_id = "route-b"
        error.failed_deployment_order = 2
        request_kwargs = {
            "model": "default-chat",
            "stream": True,
            "tools": [{"type": "image_generation"}],
            "litellm_metadata": {
                hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED_METADATA_KEY: [
                    "id:route-a",
                ]
            },
        }

        entry = hooks._ordered_deployment_fallback_entry(
            FakeRouter(),
            error,
            request_kwargs,
        )

        self.assertIsNone(entry)
        self.assertTrue(
            hooks._is_image_generation_all_deployments_unsupported_error(error)
        )

    def test_account_pool_exhaustion_is_reported_as_route_capacity(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = RuntimeError("all available accounts exhausted")
        error.status_code = 403
        event = hooks._synthesized_failed_response_event(
            {"model": "default-chat", "input": [], "stream": True},
            error,
        )
        response_error = event["response"]["error"]

        self.assertEqual(response_error["code"], "upstream_relay_capacity")
        self.assertIn("upstream relay channel unavailable", response_error["message"])
        self.assertIn("all available accounts exhausted", response_error["message"])

    def test_prompt_policy_error_is_not_fallback_eligible(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "OpenAIException invalid_request_error: prompt violates content policy"
        )
        exc.status_code = 400

        self.assertTrue(hooks._is_terminal_prompt_or_policy_error(exc))
        self.assertFalse(hooks._is_image_parameter_or_capability_bad_request_error(exc))
        self.assertFalse(hooks._is_priority_deployment_failover_error(exc))
        self.assertFalse(hooks._should_sanitize_final_upstream_route_error(exc))

    def test_deployment_order_falls_back_to_route_key_order(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_kwargs = {
            "model_info": {
                "id": "image-order2",
                "route_key": "backup_provider / openai/gpt-image-2 / key=x-image / order=2",
            }
        }

        self.assertEqual(hooks._deployment_order_from_request(request_kwargs), 2)
        self.assertTrue(hooks._request_allows_failed_deployment_order(request_kwargs))

    def test_order_parsing_preserves_zero_negative_and_fractional_values(self) -> None:
        hooks, _proxy_server = load_hook_module()

        self.assertEqual(hooks._coerce_order(0), 0)
        self.assertEqual(hooks._coerce_order("-2.5"), -2.5)
        self.assertEqual(hooks._coerce_order("0.25"), 0.25)
        self.assertIsNone(hooks._coerce_order("not-a-number"))
        self.assertEqual(
            hooks._deployment_order_from_request(
                {"model_info": {"route_key": "provider / upstream / order=-0.5"}}
            ),
            -0.5,
        )

    def test_fractional_orders_sort_and_rotate_numerically(self) -> None:
        hooks, _proxy_server = load_hook_module()
        deployments = [
            {"litellm_params": {"order": 0.25}, "model_info": {"id": "quarter"}},
            {"litellm_params": {"order": -1.5}, "model_info": {"id": "negative"}},
            {"litellm_params": {"order": 0}, "model_info": {"id": "zero"}},
        ]

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

        def original_get_all_deployments(self, model_name, team_id=None):
            return deployments

        FakeRouter._get_all_deployments._original_get_all_deployments = original_get_all_deployments
        self.assertEqual(
            hooks._configured_deployment_orders(FakeRouter(), {"model": "default-chat"}),
            [-1.5, 0, 0.25],
        )
        self.assertEqual(hooks._next_configured_order([-1.5, 0, 0.25], -1.5), 0)
        self.assertEqual(hooks._next_configured_order([-1.5, 0, 0.25], 0), 0.25)

    @staticmethod
    def _third_party_structured_compaction_request() -> dict:
        return {
            "call_type": "aresponses",
            "model": "kimi-k3",
            "stream": True,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": "SIGNED-HISTORY-MUST-NOT-REACH-THE-PROBE",
                },
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "tools": [],
            "client_metadata": {
                "session_id": "thread-compaction-test",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "model_info": {
                "id": "third-party-kimi-route",
                "provider": "third-party",
                "order": 1,
            },
            "litellm_params": {
                "model": "openai/kimi-k3",
                "api_base": "https://gateway.example.test/v1",
                "order": 1,
            },
        }

    async def test_compaction_capability_probe_is_history_free_and_cached_on_success(self) -> None:
        hooks, proxy_server = load_hook_module()
        hook = hooks.YoungRouterHook()
        calls = []
        hooks._CODEX_COMPACTION_CAPABILITIES.clear()
        self.addCleanup(hooks._CODEX_COMPACTION_CAPABILITIES.clear)

        async def supported_stream():
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "compaction",
                            "encrypted_content": "opaque-probe-result",
                        }
                    ],
                },
            }

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return supported_stream()

        proxy_server.llm_router = FakeRouter()
        await hook.async_pre_call_deployment_hook(
            self._third_party_structured_compaction_request(), "aresponses"
        )
        await hook.async_pre_call_deployment_hook(
            self._third_party_structured_compaction_request(), "aresponses"
        )

        self.assertEqual(1, len(calls))
        probe = calls[0]
        self.assertEqual(
            probe["input"],
            [
                {
                    "type": "compaction_trigger",
                    "id": "young-router-compaction-capability-probe",
                }
            ],
        )
        self.assertNotIn("SIGNED-HISTORY-MUST-NOT-REACH-THE-PROBE", str(probe))
        self.assertEqual([], probe["tools"])
        self.assertTrue(
            probe["litellm_metadata"][
                hooks._CODEX_COMPACTION_CAPABILITY_PROBE_METADATA_KEY
            ]
        )
        self.assertEqual(
            "supported",
            hooks._cached_codex_compaction_capability_status(
                self._third_party_structured_compaction_request()
            ),
        )

    async def test_unsupported_compaction_uses_cached_summary_fallback_signal(self) -> None:
        hooks, proxy_server = load_hook_module()
        hook = hooks.YoungRouterHook()
        calls = []
        hooks._CODEX_COMPACTION_CAPABILITIES.clear()
        self.addCleanup(hooks._CODEX_COMPACTION_CAPABILITIES.clear)

        async def ordinary_response_stream():
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "not compaction"}
                            ],
                        }
                    ],
                },
            }

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return ordinary_response_stream()

        proxy_server.llm_router = FakeRouter()
        for _ in range(2):
            with self.assertRaises(
                hooks.CodexCompactionCapabilityUnsupportedError
            ) as raised:
                await hook.async_pre_call_deployment_hook(
                    self._third_party_structured_compaction_request(), "aresponses"
                )
            self.assertEqual(422, raised.exception.status_code)
            self.assertEqual(
                "upstream_compaction_unsupported", raised.exception.body["error"]["code"]
            )
            self.assertIn("summary fallback", str(raised.exception))

        self.assertEqual(1, len(calls))
        self.assertEqual(
            "unsupported",
            hooks._cached_codex_compaction_capability_status(
                self._third_party_structured_compaction_request()
            ),
        )

    async def test_compaction_capability_probe_does_not_cache_transient_failures(self) -> None:
        hooks, proxy_server = load_hook_module()
        hooks._CODEX_COMPACTION_CAPABILITIES.clear()
        self.addCleanup(hooks._CODEX_COMPACTION_CAPABILITIES.clear)

        class FakeRouter:
            async def aresponses(self, **payload):
                raise TimeoutError("probe timed out")

        proxy_server.llm_router = FakeRouter()
        status = await hooks._ensure_codex_compaction_capability(
            self._third_party_structured_compaction_request()
        )

        self.assertEqual("unknown", status)
        self.assertIsNone(
            hooks._cached_codex_compaction_capability_status(
                self._third_party_structured_compaction_request()
            )
        )

    async def test_compaction_capability_probe_does_not_cache_auth_failures(self) -> None:
        hooks, proxy_server = load_hook_module()
        hooks._CODEX_COMPACTION_CAPABILITIES.clear()
        self.addCleanup(hooks._CODEX_COMPACTION_CAPABILITIES.clear)

        class FakeRouter:
            async def aresponses(self, **payload):
                error = RuntimeError("API key is invalid")
                error.status_code = 401
                raise error

        proxy_server.llm_router = FakeRouter()
        status = await hooks._ensure_codex_compaction_capability(
            self._third_party_structured_compaction_request()
        )

        self.assertEqual("unknown", status)
        self.assertIsNone(
            hooks._cached_codex_compaction_capability_status(
                self._third_party_structured_compaction_request()
            )
        )

    async def test_compaction_capability_probe_is_shared_within_a_worker(self) -> None:
        hooks, proxy_server = load_hook_module()
        hooks._CODEX_COMPACTION_CAPABILITIES.clear()
        self.addCleanup(hooks._CODEX_COMPACTION_CAPABILITIES.clear)
        calls = []

        async def supported_stream():
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "compaction",
                            "encrypted_content": "opaque-probe-result",
                        }
                    ],
                },
            }

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                await asyncio.sleep(0.02)
                return supported_stream()

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "deployment-cooldowns.json"
            self.set_env(hooks._DEPLOYMENT_COOLDOWN_FILE_ENV, str(state_path))
            proxy_server.llm_router = FakeRouter()
            statuses = await asyncio.gather(
                *(
                    hooks._ensure_codex_compaction_capability(
                        self._third_party_structured_compaction_request()
                    )
                    for _ in range(8)
                )
            )

            self.assertEqual(["supported"] * 8, statuses)
            self.assertEqual(1, len(calls))
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "supported",
                next(iter(saved["codex_compaction_capabilities"].values()))["status"],
            )

    async def test_official_openai_compaction_skips_capability_probe(self) -> None:
        hooks, proxy_server = load_hook_module()

        class UnexpectedRouter:
            async def aresponses(self, **payload):
                raise AssertionError("official OpenAI should not be capability-probed")

        proxy_server.llm_router = UnexpectedRouter()
        request = self._third_party_structured_compaction_request()
        request["litellm_params"]["api_base"] = "https://api.openai.com/v1"

        self.assertEqual(
            "skipped", await hooks._ensure_codex_compaction_capability(request)
        )

    def test_compaction_capability_memory_defaults_to_thirty_minutes(self) -> None:
        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._CODEX_COMPACTION_CAPABILITY_TTL_SECONDS_ENV, None)
        self.assertEqual(1800.0, hooks._codex_compaction_capability_ttl_seconds())
        self.set_env(hooks._CODEX_COMPACTION_CAPABILITY_TTL_SECONDS_ENV, "0")
        self.assertEqual(0.0, hooks._codex_compaction_capability_ttl_seconds())

    def test_local_stream_timeouts_are_not_labeled_as_upstream_status(self) -> None:
        hooks, _proxy_server = load_hook_module()

        start_timeout = TimeoutError(
            "Young Router stream start timeout after 120s without the first stream event"
        )
        start_timeout.status_code = 504
        start_timeout.body = {
            "reason": "stream_start_timeout",
            "start_seconds": 120.0,
            "saw_chunk": False,
            "buffered_chunks": 0,
        }
        self.assertEqual(
            hooks._trace_exception(start_timeout)["reason"],
            "stream_start_timeout",
        )

        idle_timeout = TimeoutError(
            "Young Router stream idle timeout after 120s without a new chunk"
        )
        idle_timeout.status_code = 504
        idle_timeout.body = {"reason": "stream_idle_timeout", "saw_chunk": True}
        self.assertEqual(
            hooks._trace_exception(idle_timeout)["reason"],
            "stream_idle_timeout",
        )

        # A genuine upstream 504 without a local watchdog body keeps the
        # upstream-status label.
        upstream_timeout = TimeoutError("gateway timeout")
        upstream_timeout.status_code = 504
        self.assertEqual(
            hooks._trace_exception(upstream_timeout)["reason"],
            "upstream-status-504",
        )

    def _real_compaction_request(self) -> dict:
        return {
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger"},
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "api_base": "https://relay.example/v1",
            "model_info": {"id": "route-relay"},
        }

    def test_real_compaction_400_on_probed_supported_route_marks_unsupported(self) -> None:
        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._CODEX_COMPACTION_CAPABILITY_TTL_SECONDS_ENV, "600")
        self.addCleanup(hooks._CODEX_COMPACTION_CAPABILITIES.clear)
        request = self._real_compaction_request()
        cache_key = hooks._codex_compaction_capability_cache_key(request)
        now = time.time()
        with hooks._CODEX_COMPACTION_CAPABILITY_LOCK:
            hooks._CODEX_COMPACTION_CAPABILITIES[cache_key] = {
                "status": "supported",
                "deployment_id": "route-relay",
                "route_key": "k",
                "detected_at": now,
                "expires_at": now + 600,
            }

        exception = RuntimeError(
            "litellm.badrequesterror: openaiexception - upstream request failed"
        )
        exception.status_code = 400
        self.assertTrue(
            hooks._mark_real_compaction_failure_capability_unsupported(
                request,
                exception,
            )
        )
        self.assertTrue(
            hooks._is_codex_compaction_capability_unsupported_error(exception)
        )
        with hooks._CODEX_COMPACTION_CAPABILITY_LOCK:
            state = hooks._CODEX_COMPACTION_CAPABILITIES.get(cache_key)
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "unsupported")

    def test_compaction_route_classification_prefers_selected_nested_api_base(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request = self._real_compaction_request()
        # LiteLLM can retain the prior route's top-level api_base while the
        # selected deployment is represented in nested parameters. Capability
        # classification must follow the selected route without mutating the
        # client-facing top-level request.
        request["api_base"] = "https://api.openai.com/v1"
        request["litellm_params"] = {
            "model": "openai/vendor-model",
            "api_base": "https://relay.example/v1",
        }
        self.assertTrue(
            hooks._request_uses_third_party_codex_compaction_route(request)
        )

    def test_real_compaction_marking_guards(self) -> None:
        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._CODEX_COMPACTION_CAPABILITY_TTL_SECONDS_ENV, "600")
        self.addCleanup(hooks._CODEX_COMPACTION_CAPABILITIES.clear)
        request = self._real_compaction_request()

        # 500（服务端/瞬态）不算证据
        server_error = RuntimeError("boom")
        server_error.status_code = 500
        self.assertFalse(
            hooks._mark_real_compaction_failure_capability_unsupported(
                request, server_error
            )
        )
        # 签名类确定性错误不碰
        signature_error = RuntimeError("thinking_signature_invalid")
        signature_error.status_code = 400
        self.assertFalse(
            hooks._mark_real_compaction_failure_capability_unsupported(
                request, signature_error
            )
        )
        # 非第三方路由（官方 OpenAI 或探针请求）不标记
        official = dict(request, api_base="https://api.openai.com/v1")
        official_error = RuntimeError("upstream request failed")
        official_error.status_code = 400
        self.assertFalse(
            hooks._mark_real_compaction_failure_capability_unsupported(
                official, official_error
            )
        )
        probe = {
            **request,
            "litellm_metadata": {
                hooks._CODEX_COMPACTION_CAPABILITY_PROBE_METADATA_KEY: True,
            },
        }
        probe_error = RuntimeError("upstream request failed")
        probe_error.status_code = 400
        self.assertFalse(
            hooks._mark_real_compaction_failure_capability_unsupported(
                probe, probe_error
            )
        )
        # 缓存不是 supported 时不标记
        uncached_error = RuntimeError("upstream request failed")
        uncached_error.status_code = 400
        self.assertFalse(
            hooks._mark_real_compaction_failure_capability_unsupported(
                request, uncached_error
            )
        )
        # An explicit context-size rejection remains an input-size failure,
        # even if the gateway mentions a custom tool call in the body.
        context_error = RuntimeError(
            "no tool output found for custom tool call; context length exceeded"
        )
        context_error.status_code = 400
        self.assertFalse(
            hooks._mark_real_compaction_failure_capability_unsupported(
                request, context_error
            )
        )


    def test_sanitized_failure_attributes_relay_channel_exhaustion(self) -> None:
        """A relay-capacity 400 must blame the relay, not the requested model.

        relay-style gateways reject requests with a well-formed 400 whose body
        says their upstream channel pool is exhausted.  The sanitized message
        must keep the relay's own wording and a relay-specific reason so logs
        never read as if kimi-k3 itself failed.
        """
        hooks, _proxy_server = load_hook_module()

        class BadRequest(Exception):
            pass

        relay_exc = BadRequest(
            'litellm.badrequesterror: openaiexception - {"error":{"message":'
            '"请稍后重试，暂无可用渠道，或切换模型 (request id: x)","type":'
            '"capability_not_supported"}}'
        )
        relay_exc.status_code = 400
        message = hooks._sanitized_upstream_route_failure_message(
            "kimi-k3",
            relay_exc,
            {"model": "kimi-k3"},
        )
        self.assertIn("upstream relay channel unavailable", message)
        self.assertIn("暂无可用渠道", message)
        self.assertIn("[upstream relay reported:", message)
        self.assertIn("kimi-k3", message)
        self.assertTrue(hooks._is_upstream_relay_capacity_error(relay_exc))

        plain_exc = BadRequest("litellm.badrequesterror: some unrelated 400")
        plain_exc.status_code = 400
        self.assertFalse(hooks._is_upstream_relay_capacity_error(plain_exc))
        plain_message = hooks._sanitized_upstream_route_failure_message(
            "kimi-k3",
            plain_exc,
            {"model": "kimi-k3"},
        )
        self.assertNotIn("upstream relay channel unavailable", plain_message)
        self.assertNotIn("[upstream relay reported:", plain_message)

    def test_a_route_refusing_the_replayed_tool_history_keeps_looking(self) -> None:
        """A strict route's history check is about this replay, not the model.

        Observed on the local worker after a turn was cut between a tool call
        and its output: it answered HTTP 400
        `workbuddy upstream client (http 400): 工具记录不完整，请新建任务`
        ("tool records incomplete, create a new task").  Read as the request's
        own verdict it ended the turn on that route with the unclassified-stop
        sentence, so the client re-sent the identical history and met it again.
        A peer may accept the same replay, so the attempt advances; only a
        refusal that reaches no accepted route names the cause and the new task.
        """

        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_POLICY_REQUEST_ERROR_ENV, None)

        class BadRequestError(Exception):
            pass

        rejected = BadRequestError(
            "litellm.BadRequestError: OpenAIException - workbuddy upstream "
            "client (http 400): 工具记录不完整，请新建任务 {'message': "
            "'workbuddy upstream client (http 400): 工具记录不完整，请新建任务', "
            "'type': 'client', 'code': 'client'}"
        )
        rejected.status_code = 400

        self.assertTrue(hooks._is_upstream_tool_history_rejected_error(rejected))
        self.assertEqual(
            hooks._recovery_policy_for_exception(rejected),
            "recovery",
        )
        self.assertTrue(hooks._is_priority_deployment_failover_error(rejected))
        self.assertEqual(
            hooks._trace_exception(rejected)["reason"],
            "upstream-tool-history-rejected",
        )
        self.assertEqual(
            hooks._recovery_diagnostic(rejected)["kind"],
            "tool_history_rejected",
        )
        sanitized = hooks._sanitized_upstream_route_failure_message(
            "balanced-chat",
            rejected,
            {"model": "balanced-chat"},
        )
        self.assertIn("tool records", sanitized)
        self.assertIn("new task", sanitized)
        self.assertNotIn("retry later", sanitized.lower())

        # The same verdict in a station's English wording is the same cause,
        # not an anonymous request error.
        english = BadRequestError(
            "litellm.BadRequestError: OpenAIException - tool calls and tool "
            "results do not match, please start a new conversation and retry "
            "(request id: 202610221208000799135708268d9d6hqsg9zze)"
        )
        english.status_code = 400
        self.assertTrue(hooks._is_upstream_tool_history_rejected_error(english))
        self.assertEqual(hooks._recovery_policy_for_exception(english), "recovery")

        # A refusal that does not name the history keeps the ordinary verdict.
        plain = BadRequestError("litellm.BadRequestError: some unrelated 400")
        plain.status_code = 400
        self.assertFalse(hooks._is_upstream_tool_history_rejected_error(plain))
        self.assertEqual(hooks._recovery_policy_for_exception(plain), "error")

    def test_a_route_refusing_its_own_account_advances_instead_of_ending_the_turn(
        self,
    ) -> None:
        """An account refusal is the route's verdict, not the request's.

        Observed on a local worker whose upstream answered HTTP 400
        ``workbuddy upstream client: 账号存在异常行为，已被限制使用`` ("abnormal account
        behavior, use restricted").  Classified as a request error it was
        terminal: the dead route was never cooled, every client retry selected
        it first again, and the turn ended on the unclassified-stop sentence
        while a sibling route of the same group answered the same bytes.  The
        refusal is one route's account, so the turn advances and the route is
        parked; only a replay that reaches no other route names the account.
        """

        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_POLICY_REQUEST_ERROR_ENV, None)

        class BadRequestError(Exception):
            pass

        restricted = BadRequestError(
            "litellm.BadRequestError: OpenAIException - workbuddy upstream "
            "client: 账号存在异常行为，已被限制使用 litellm.BadRequestError: "
            "OpenAIException - workbuddy upstream client: 账号存在异常行为，已被限制使用 "
            "{'message': 'workbuddy upstream client: 账号存在异常行为，已被限制使用', "
            "'type': 'client', 'code': 'client'}"
        )
        restricted.status_code = 400

        self.assertTrue(hooks._is_upstream_account_restricted_error(restricted))
        self.assertEqual(
            hooks._recovery_policy_for_exception(restricted),
            "recovery_cooldown",
        )
        self.assertTrue(
            hooks._is_priority_deployment_failover_error(restricted)
        )
        self.assertEqual(
            hooks._trace_exception(restricted)["reason"],
            "upstream-account-restricted",
        )
        self.assertEqual(
            hooks._recovery_diagnostic(restricted)["kind"],
            "account_restricted",
        )
        # The final failure for a chat client keeps the same cause and never
        # turns the refusal into a retry tip.
        sanitized = hooks._sanitized_upstream_route_failure_message(
            "balanced-chat",
            restricted,
            {"model": "balanced-chat"},
        )
        self.assertIn("restricted", sanitized)
        self.assertIn("balanced-chat", sanitized)
        self.assertNotIn("retry later", sanitized.lower())
        self.assertNotIn("temporary", sanitized.lower())

        # An ordinary 400 stays the request's own verdict: it neither parks the
        # route nor moves the turn to a peer.
        invalid = BadRequestError("litellm.BadRequestError: some unrelated 400")
        invalid.status_code = 400
        self.assertFalse(hooks._is_upstream_account_restricted_error(invalid))
        self.assertEqual(hooks._recovery_policy_for_exception(invalid), "error")
        self.assertFalse(hooks._is_priority_deployment_failover_error(invalid))

    def test_a_route_whose_own_channel_client_is_stale_advances_and_is_parked(
        self,
    ) -> None:
        """A stale channel client is the route's verdict, not the request's.

        Observed as 18 identical failures over one model group: the first route
        in the order answered every call with HTTP 400
        ``Claude Code 2.1.63 does not support this model; version 2.1.280 or
        newer is required``.  The status alone read as the request's own
        verdict, so ``recovery_policy`` stayed ``error``: the route was never
        cooled, every subsequent request selected it first again, and the turn
        ended on it while siblings of the same group served the identical bytes.
        """

        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_POLICY_REQUEST_ERROR_ENV, None)

        class BadRequestError(Exception):
            pass

        stale = BadRequestError(
            'litellm.BadRequestError: OpenAIException - {"error":{"code":"server_error",'
            '"message":"Claude Code 2.1.63 does not support this model; version '
            '2.1.280 or newer is required. Run \'claude update\', or update the '
            'Claude desktop app, then try again."}}'
        )
        stale.status_code = 400

        self.assertTrue(hooks._is_upstream_client_version_rejected_error(stale))
        self.assertEqual(
            hooks._recovery_policy_for_exception(stale),
            "recovery_cooldown",
        )
        # The route is parked and the turn advances to a sibling that can answer.
        self.assertTrue(hooks._is_priority_deployment_failover_error(stale))
        self.assertTrue(hooks._should_count_deployment_failure_for_cooldown(stale))
        self.assertEqual(
            hooks._trace_exception(stale)["reason"],
            "upstream-client-version-rejected",
        )
        self.assertEqual(
            hooks._recovery_diagnostic(stale)["kind"],
            "upstream_client_outdated",
        )
        # The final stop names the route's own cause and never invites the same
        # bytes back with a retry tip.
        sanitized = hooks._sanitized_upstream_route_failure_message(
            "claude-opus-5-5",
            stale,
            {"model": "claude-opus-5-5"},
        )
        self.assertIn("too old", sanitized)
        self.assertIn("claude-opus-5-5", sanitized)
        self.assertNotIn("retry later", sanitized.lower())
        self.assertNotIn("temporary", sanitized.lower())

        # A plain "not supported" 400 is a different refusal: without a named
        # stale client it stays the request's own verdict.
        unsupported = BadRequestError(
            'litellm.BadRequestError: OpenAIException - {"error":{"message":'
            '"this model does not support tool calls"}}'
        )
        unsupported.status_code = 400
        self.assertFalse(
            hooks._is_upstream_client_version_rejected_error(unsupported)
        )
        self.assertEqual(
            hooks._recovery_policy_for_exception(unsupported), "error"
        )
        # The version sentence without a named client is not evidence either.
        anonymous = BadRequestError(
            "litellm.BadRequestError: version 2.1.280 or newer is required"
        )
        anonymous.status_code = 400
        self.assertFalse(
            hooks._is_upstream_client_version_rejected_error(anonymous)
        )

    def test_a_station_without_a_price_for_the_model_is_parked_not_answered(
        self,
    ) -> None:
        """A station's missing price entry is the route's verdict, not ours.

        Observed live: one station answered every request for a model with HTTP
        400 ``Model claude-fable-5-2 has not been priced by the administrator
        yet``.  The status alone read as the request's own verdict, so
        ``recovery_policy`` stayed ``error``: the turn ended on the first route,
        the raw 400 reached the client as an invalid request, and the route was
        never parked, so every later turn paid the same refusal while siblings
        of the same group served the identical model.
        """

        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_POLICY_REQUEST_ERROR_ENV, None)

        class BadRequestError(Exception):
            pass

        unpriced = BadRequestError(
            'litellm.BadRequestError: AnthropicException - {"type":"new_api_error",'
            '"message":"Model claude-fable-5-2 has not been priced by the '
            'administrator yet. Please contact the site administrator to enable '
            'this model. (request id: 20261111040055327528536826bd9d65mx2kKB1)"}'
        )
        unpriced.status_code = 400

        self.assertTrue(hooks._is_upstream_model_unpriced_error(unpriced))
        self.assertEqual(
            hooks._recovery_policy_for_exception(unpriced),
            "recovery_cooldown",
        )
        # The route is parked and this attempt advances to a sibling that can
        # answer the same model.
        self.assertTrue(hooks._is_priority_deployment_failover_error(unpriced))
        self.assertTrue(
            hooks._should_count_deployment_failure_for_cooldown(unpriced)
        )
        self.assertEqual(
            hooks._trace_exception(unpriced)["reason"],
            "upstream-model-unpriced",
        )
        self.assertEqual(
            hooks._recovery_diagnostic(unpriced)["kind"],
            "upstream_model_unpriced",
        )
        # A final stop for this cause must name it instead of forwarding the
        # station's raw 400 as the request's own invalid-request verdict.
        self.assertTrue(hooks._should_sanitize_final_upstream_route_error(unpriced))
        sanitized = hooks._sanitized_upstream_route_failure_message(
            "claude-fable-5-2",
            unpriced,
            {"model": "claude-fable-5-2"},
        )
        self.assertIn("price", sanitized.lower())
        self.assertIn("claude-fable-5-2", sanitized)
        lowered = sanitized.lower()
        for retry_tip in ("retry", "try again", "send the message again"):
            self.assertNotIn(retry_tip, lowered)

        # New API's admin branch states the same refusal in Chinese.
        admin = BadRequestError(
            'litellm.BadRequestError: AnthropicException - {"error":{"message":'
            '"模型 claude-fable-5-2 的价格未配置。请前往「系统设置 → 运营设置」开启自用模式；'
            'Model claude-fable-5-2 price not configured."}}'
        )
        admin.status_code = 400
        self.assertTrue(hooks._is_upstream_model_unpriced_error(admin))
        self.assertEqual(
            hooks._recovery_policy_for_exception(admin),
            "recovery_cooldown",
        )

        # A model this route simply does not serve is a different refusal: it
        # stays the request's own verdict.
        unsupported = BadRequestError(
            'litellm.BadRequestError: AnthropicException - {"error":{"message":'
            '"this model does not support tool calls"}}'
        )
        unsupported.status_code = 400
        self.assertFalse(hooks._is_upstream_model_unpriced_error(unsupported))
        self.assertEqual(
            hooks._recovery_policy_for_exception(unsupported), "error"
        )

    def test_a_request_row_states_the_mode_the_client_chose(self) -> None:
        """The row reports the client's own stream flag, not a bridge's rewrite.

        Claude Code streams, and falls back to a non-streaming request only
        after a stream fails (live 2026-11-11: a mid-stream relay
        ``upstream_error`` pushed it into that fallback, and the log had no
        column that could tell the fallback apart from the turn it replaced).
        The mode must therefore be read from the body the client sent: a
        station's web-search interception rewrites ``stream`` to false on the
        way upstream, and the row must not report that rewrite as the client's
        choice.
        """

        hooks, _proxy_server = load_hook_module()
        kwargs = {
            "model": "claude-fable-5-2",
            "stream": True,
            "proxy_server_request": {
                "body": {
                    "model": "claude-fable-5-2",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                }
            },
        }

        self.assertIs(False, hooks._request_log_streamed(kwargs))
        self.assertIs(
            False,
            hooks._request_log_record("success", kwargs).get("stream"),
        )
        self.assertIs(True, hooks._request_log_streamed({"stream": True}))
        # A record that states no mode keeps no key: the pane leaves the cell
        # empty instead of guessing the protocol's default into a column.
        self.assertIsNone(hooks._request_log_streamed({}))

    async def test_filter_deployments_drops_a_route_whose_address_never_resolved(self) -> None:
        """A worker this host never staged is not a route it can serve.

        ``young_router.adapters.managed`` resolves a bundled worker's loopback
        origin in the environment it execs the proxy with.  A host that never
        staged that worker sets nothing, and LiteLLM deletes ``api_base`` and
        ``api_key`` from the deployment rather than blanking them -- so what
        reaches the filter is a route with no address and a route key that still
        records the reference it was written from.  Offering that route costs
        every turn a round trip that ends in ``Missing credentials``.
        """

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env("YOUNG_ROUTER_TEST_UNSET_BASE", None)
        resolved = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://api.backup.example/v1",
                "order": 2,
            },
            "model_info": {
                "id": "resolved-route",
                "provider": "backup_provider",
                "route_key": "model=default-chat / provider=backup_provider / host=api.backup.example / order=2",
            },
        }
        still_a_reference = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "os.environ/YOUNG_ROUTER_TEST_UNSET_BASE",
                "api_key": "os.environ/YOUNG_ROUTER_TEST_UNSET_KEY",
                "order": 1,
            },
            "model_info": {"id": "reference-route", "provider": "wb"},
        }
        resolved_by_litellm_and_lost = {
            "litellm_params": {"model": "openai/default-chat", "order": 1},
            "model_info": {
                "id": "lost-route",
                "provider": "wb",
                "route_key": "model=default-chat / provider=wb / host=os.environ / key=workbuddy-local / order=1",
            },
        }

        filtered = await hook.async_filter_deployments(
            "default-chat",
            [still_a_reference, resolved_by_litellm_and_lost, resolved],
            messages=[],
            request_kwargs={"call_type": "acompletion", "stream": True},
        )

        self.assertEqual([item["model_info"]["id"] for item in filtered], ["resolved-route"])

    async def test_filter_deployments_keeps_a_reference_this_process_resolved(self) -> None:
        """The desktop side of the same rule: a published reference is a route."""

        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        self.set_env("YOUNG_ROUTER_TEST_SET_BASE", "http://127.0.0.1:45870/v1")
        deployment = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "os.environ/YOUNG_ROUTER_TEST_SET_BASE",
                "api_key": "os.environ/YOUNG_ROUTER_TEST_SET_KEY",
                "order": 1,
            },
            "model_info": {"id": "managed-route", "provider": "wb"},
        }

        filtered = await hook.async_filter_deployments(
            "default-chat",
            [deployment],
            messages=[],
            request_kwargs={"call_type": "acompletion", "stream": True},
        )

        self.assertEqual(filtered, [deployment])

    def test_a_station_channel_refusal_is_not_the_routes_credential(self) -> None:
        """The station answers for its channel, not for the key we hold.

        flux-code (sub2api) sends ``{"type":"error","error":{"type":
        "upstream_error","message":"Upstream access forbidden, please contact
        administrator"}}`` as an SSE frame after the route already generated.
        The key was accepted and the station picks a channel per attempt, so a
        resend can reach one that answers.  Read as our credential (live
        2026-11-12) this parked the only deployment, and every retry then failed
        locally with "no deployments available" -- the fast failures.
        """

        hooks, _proxy_server = load_hook_module()
        streaming_module = sys.modules["young_router.proxy.streaming"]

        exception = streaming_module._stream_chunk_error_exception(
            b'event: error\n'
            b'data: {"type":"error","error":{"type":"upstream_error","message":'
            b'"Upstream access forbidden, please contact administrator"}}\n\n'
        )

        self.assertTrue(hooks._is_upstream_channel_refused_error(exception))
        self.assertFalse(hooks._is_upstream_session_rejected_error(exception))
        self.assertEqual(
            hooks._RECOVERY_POLICY_RECOVERY,
            hooks._recovery_policy_for_exception(exception),
        )
        self.assertFalse(
            hooks._should_count_deployment_failure_for_cooldown(exception)
        )
        self.assertEqual(
            "upstream-channel-refused",
            hooks._trace_exception(exception)["reason"],
        )
        self.assertEqual(
            "upstream_channel_refused",
            hooks._recovery_diagnostic(exception)["kind"],
        )

        message = hooks._sanitized_upstream_route_failure_message(
            "claude-fable-5-2(cursor)",
            exception,
            {"model": "claude-fable-5-2(cursor)"},
        )
        # The sentence has to keep the client resending -- the phrase list Pi
        # matches is the only thing that revives the turn -- and it may not
        # repeat the two false claims of the credential wording.
        self.assertIn("ended without", message)
        self.assertIn("station", message)
        self.assertNotIn("credential", message)
        self.assertNotIn("refresh that route's account", message)
        self.assertNotIn("another route will answer", message)

        # A real credential refusal keeps its own branch and its own policy.
        credential = RuntimeError(
            "litellm.BadRequestError: OpenAIException - request rejected by "
            "upstream (http 401): invalid_api_key"
        )
        credential.status_code = 400
        self.assertTrue(hooks._is_upstream_session_rejected_error(credential))
        self.assertFalse(hooks._is_upstream_channel_refused_error(credential))
        self.assertEqual(
            hooks._RECOVERY_POLICY_COOLDOWN,
            hooks._recovery_policy_for_exception(credential),
        )

    def test_an_empty_pool_is_not_charged_to_the_only_route(self) -> None:
        """The pool answer for *this request* is not a failure of a route.

        Every candidate is either excluded by this attempt or cooling for its
        own recorded failure.  Charging the wrapper parks a route no upstream
        refused and, on a single-route group, erases the only route from the
        very attempt that was about to retry it (live 2026-11-12: the poll's
        local round recorded a 45 s cooldown, so every later round answered
        locally and the turn ended in five seconds).
        """

        hooks, _proxy_server = load_hook_module()

        class RouterRateLimitError(Exception):
            pass

        exception = RouterRateLimitError(
            "no deployments available for selected model, try again in 45 "
            "seconds. passed model=claude-fable-5-2(cursor)"
        )
        exception.status_code = 429

        self.assertTrue(hooks._is_no_deployments_available_error(exception))
        self.assertEqual(
            hooks._RECOVERY_POLICY_RECOVERY,
            hooks._recovery_policy_for_exception(exception),
        )
        self.assertFalse(
            hooks._should_count_deployment_failure_for_cooldown(exception)
        )
        self.assertEqual(
            "no-available-deployment", hooks._trace_exception(exception)["reason"]
        )

    def test_a_pool_answer_does_not_name_the_route_it_never_reached(self) -> None:
        """Marking "no deployments available" must not exclude the only route.

        The wrapper is raised at the end of an attempt, so the request still
        carries the deployment it was pointed at; attributing that id to the
        wrapper makes ``_sync_failed_deployment_exclusions`` add the route to
        this request's exclusions on every recovery round.  On a single-route
        group that is the whole pool: each round then answers locally while the
        router's own cooldown was the only thing keeping the route out (live
        2026-11-12: two rounds, 5.2 s, no upstream contact).
        """

        hooks, _proxy_server = load_hook_module()

        class RouterRateLimitError(Exception):
            pass

        exception = RouterRateLimitError(
            "no deployments available for selected model, try again in 45 "
            "seconds. passed model=claude-fable-5-2(cursor)"
        )
        exception.status_code = 429
        request_data = {
            "model": "claude-fable-5-2(cursor)",
            "deployment_id": "relay-only",
            "model_info": {"id": "relay-only", "order": 0.35},
            "litellm_metadata": {},
        }

        hooks._mark_exception_for_deployment_failover(exception, request_data)

        self.assertIsNone(getattr(exception, "failed_deployment_id", None))
        self.assertEqual(
            [],
            list(request_data.get("_excluded_deployment_ids") or []),
        )
        self.assertEqual(
            [],
            list(
                (request_data.get("litellm_metadata") or {}).get(
                    "excluded_deployment_ids"
                )
                or []
            ),
        )

    def test_a_single_route_group_is_never_promised_a_sibling(self) -> None:
        """One configured route cannot answer "another route will answer".

        The sentence a client renders decides whether it resends, so promising
        a sibling that does not exist (and telling the reader to resend into the
        same parked route) is how a single-route group produced a retry button
        that could only meet "no deployments available" (live 2026-11-12).
        """

        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            def __init__(self, count: int) -> None:
                self.deployments = [
                    {"model_info": {"id": f"route-{index}"}}
                    for index in range(count)
                ]

            def _get_all_deployments(self, model_name, team_id=None):
                return list(self.deployments)

        request = {"model": "claude-fable-5-2(cursor)"}
        channel_refused = RuntimeError(
            "upstream access forbidden, please contact administrator "
            "type=upstream_error"
        )
        channel_refused.status_code = 502
        credential_refused = RuntimeError(
            "workbuddy upstream client (http 401): invalid token"
        )
        credential_refused.status_code = 400

        previous_router = getattr(proxy_server, "llm_router", None)
        self.addCleanup(setattr, proxy_server, "llm_router", previous_router)

        proxy_server.llm_router = FakeRouter(1)
        self.assertEqual(
            1, hooks._request_group_deployment_count(request)
        )
        single_channel = hooks._sanitized_upstream_route_failure_message(
            "claude-fable-5-2(cursor)",
            channel_refused,
            request,
        )
        self.assertIn("no other route", single_channel)
        self.assertNotIn("another route will answer", single_channel)
        single_credential = hooks._sanitized_upstream_route_failure_message(
            "claude-fable-5-2(cursor)",
            credential_refused,
            request,
        )
        self.assertNotIn("another route will answer", single_credential)
        self.assertIn("resending meets the same refusal", single_credential)
        # A refusal no resend can clear must not carry a phrase Pi resends on.
        self.assertNotIn("ended without", single_credential)

        proxy_server.llm_router = FakeRouter(2)
        self.assertEqual(
            2, hooks._request_group_deployment_count(request)
        )
        multi_channel = hooks._sanitized_upstream_route_failure_message(
            "claude-fable-5-2(cursor)",
            channel_refused,
            request,
        )
        self.assertNotIn("no other route", multi_channel)
        self.assertIn("ended without", multi_channel)
        multi_credential = hooks._sanitized_upstream_route_failure_message(
            "claude-fable-5-2(cursor)",
            credential_refused,
            request,
        )
        self.assertIn("another route will answer", multi_credential)

    def test_a_sanitized_route_failure_is_not_reworded_by_its_wrapper(self) -> None:
        """Re-sanitizing the wrapper replaces the cause with its own status.

        Live 2026-11-12: the route-recovery poll's terminal event was built from
        the sanitized ``ServiceUnavailableError``, so the sentence was derived a
        second time.  Its status (503) chose "temporary upstream server error"
        and the station refusal the reader could act on disappeared from the
        client's copy while the trace still named it.
        """

        hooks, _proxy_server = load_hook_module()
        streaming_module = sys.modules["young_router.proxy.streaming"]

        original = RuntimeError(
            "upstream access forbidden, please contact administrator "
            "type=upstream_error"
        )
        original.status_code = 502
        request = {
            "model": "claude-fable-5-2(cursor)",
            "stream": True,
            "proxy_server_request": {"path": "/v1/responses"},
        }
        sanitized = hooks._sanitized_upstream_route_exception(
            "claude-fable-5-2(cursor)",
            original,
            request,
        )
        self.assertTrue(hooks._is_sanitized_upstream_route_failure_error(sanitized))

        message = hooks._sanitized_upstream_route_failure_message(
            "claude-fable-5-2(cursor)",
            sanitized,
            request,
        )
        self.assertEqual(str(sanitized), message)
        self.assertIn("station", message)
        self.assertNotIn("temporary upstream server error", message)

        event = streaming_module._synthesized_failed_response_event(
            request,
            sanitized,
            after_visible_output=True,
        )
        payload = event if isinstance(event, dict) else json.loads(event.data or "{}")
        error = payload["response"]["error"]
        self.assertIn("station", error["message"])
        self.assertNotIn("temporary upstream server error", error["message"])
        self.assertEqual("upstream_route_failure", error["code"])

        # The chat wire has to agree with the sentence: a wrapper's own class
        # says nothing about the cause, and one client reads the code.
        chunk = streaming_module._synthesized_native_stream_error_chunk(
            {
                "model": "claude-fable-5-2(cursor)",
                "messages": [{"role": "user", "content": "Continue."}],
                "stream": True,
                "proxy_server_request": {"path": "/v1/chat/completions"},
            },
            sanitized,
        )
        self.assertEqual("upstream_route_failure", chunk["error"]["code"])
        self.assertIn("station", chunk["error"]["message"])
        self.assertNotIn("temporary upstream server error", chunk["error"]["message"])


if __name__ == "__main__":
    unittest.main()
