"""One route's concurrency cap, and the hop it causes.

A route that already serves as many requests as it may is skipped the way a
cooled route is, so the same public model's next route answers.  A model whose
every route is at its cap is refused with a reason of this proxy's own: it is
not an upstream rate limit (nothing upstream was asked), and it is not a reason
to move the turn to a different model.

The count is shared through a file every worker locks, so these tests run
against that file rather than against process memory: a counter kept in one
worker's memory is not a cap, it is a cap times the number of workers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap

from hook_test_utils import *

ROOT = Path(__file__).resolve().parents[1]


def _deployment(deployment_id: str, *, order: int = 1, max_concurrency: object = None) -> dict:
    model_info: dict[str, object] = {"id": deployment_id, "order": order}
    if max_concurrency is not None:
        model_info["max_concurrency"] = max_concurrency
    return {
        "litellm_params": {"model": f"openai/{deployment_id}", "order": order},
        "model_info": model_info,
    }


def _request(turn: str = "turn-1", *, deployment_id: str = "route-a") -> dict:
    return {
        "model": "default-chat",
        "stream": True,
        "input": [{"role": "user", "content": "hello"}],
        "_young_router_attempt_lease_id": turn,
        "litellm_params": {"model": f"openai/{deployment_id}", "order": 1},
        "model_info": {"id": deployment_id, "order": 1},
    }


class ConcurrencyStoreTestCase(HookTestCase):
    """Loads the hook module against a private leases file.

    The file is the store, so isolating it is isolating the test: one test's
    lease can never answer another test's claim, and nothing here depends on the
    host's own runtime directory.
    """

    def setUp(self) -> None:
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.leases_path = Path(self.directory.name) / "deployment-concurrency.json"
        self.set_env(
            "YOUNG_ROUTER_DEPLOYMENT_CONCURRENCY_FILE", str(self.leases_path)
        )
        self.set_env("YOUNG_ROUTER_MODEL_CONCURRENCY_LIMIT", "8")

    def leases(self) -> dict:
        if not self.leases_path.exists():
            return {}
        return json.loads(self.leases_path.read_text(encoding="utf-8")).get("leases", {})


class ModelConcurrencyTests(ConcurrencyStoreTestCase):
    def test_the_global_limit_defaults_to_eight(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, None)

        self.assertEqual(hooks._model_concurrency_limit(), 8)
        self.assertEqual(hooks._model_concurrency_cap(_deployment("route-a")), 8)

    def test_a_route_states_its_own_cap_and_zero_is_unlimited(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "3")

        self.assertEqual(
            hooks._model_concurrency_cap(_deployment("route-a", max_concurrency=2)), 2
        )
        # An explicit 0 is a choice, not an absent value: this route is not
        # capped even though the runtime default is.
        self.assertEqual(
            hooks._model_concurrency_cap(_deployment("route-a", max_concurrency=0)), 0
        )
        # A route that states nothing follows the global limit, which is what
        # makes changing that setting move every route that never chose one.
        self.assertEqual(hooks._model_concurrency_cap(_deployment("route-a")), 3)

    async def test_a_full_route_leaves_the_candidate_pool(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        hook = hooks.YoungRouterHook()
        deployments = [_deployment("route-a"), _deployment("route-b")]
        busy = _request("turn-busy", deployment_id="route-a")

        self.assertTrue(hooks._claim_deployment_concurrency_slot(busy, deployments[0], 1))

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs=_request("turn-new"),
        )

        self.assertEqual([item["model_info"]["id"] for item in filtered], ["route-b"])

    async def test_a_group_with_one_route_keeps_it_and_is_refused_instead(self) -> None:
        """The only route of a model is never filtered out of its own request."""

        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        hook = hooks.YoungRouterHook()
        deployments = [_deployment("route-only")]
        busy = _request("turn-busy", deployment_id="route-only")

        self.assertTrue(hooks._claim_deployment_concurrency_slot(busy, deployments[0], 1))

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs=_request("turn-new", deployment_id="route-only"),
        )

        # Still offered, so routing never reports a configured model as one
        # that has no deployments.  The refusal happens at the attempt.
        self.assertEqual([item["model_info"]["id"] for item in filtered], ["route-only"])

    def test_the_refusal_names_the_limit_and_is_not_an_upstream_rate_limit(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "2")
        deployment = _deployment("route-a")

        error = hooks._model_concurrency_rejection_exception(_request(), deployment)

        self.assertTrue(hooks._is_model_concurrency_rejection_error(error))
        self.assertIn("concurrency limit reached", str(error))
        self.assertIn("2 request(s)", str(error))
        # Not a 429: nothing upstream was asked, and a client that read this as
        # an upstream limit would back off the model instead of the busy route.
        self.assertIsNone(hooks._exception_status_code(error))
        self.assertEqual(
            hooks._trace_exception(error)["reason"], "model-concurrency-limit"
        )

    def test_the_refusal_may_advance_to_a_peer_but_never_cools_the_route(self) -> None:
        hooks, _ = load_hook_module()
        deployment = _deployment("route-a")
        error = hooks._model_concurrency_rejection_exception(_request(), deployment)

        self.assertTrue(hooks._is_priority_deployment_failover_error(error))
        # A cooldown would remove the route from its own next request; a busy
        # route is eligible again the moment one of its requests finishes.
        self.assertEqual(
            hooks._recovery_policy_for_exception(error), hooks._RECOVERY_POLICY_RECOVERY
        )

    def test_a_fallback_target_is_never_a_full_route(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        deployments = [_deployment("route-a"), _deployment("route-b", order=2)]
        busy = _request("turn-busy", deployment_id="route-a")
        self.assertTrue(hooks._claim_deployment_concurrency_slot(busy, deployments[0], 1))

        available, saturated, filtered = hooks._with_active_model_concurrency(
            deployments,
            request_kwargs=_request("turn-new"),
        )

        self.assertEqual([item["model_info"]["id"] for item in available], ["route-b"])
        self.assertEqual([item["id"] for item in saturated], ["route-a"])
        self.assertTrue(filtered)

    def test_a_hop_releases_the_slot_the_failed_attempt_held(self) -> None:
        """A real failure frees its slot instead of waiting for the lease TTL."""

        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        deployment = _deployment("route-a")
        first = _request("turn-1", deployment_id="route-a")

        self.assertTrue(hooks._claim_deployment_concurrency_slot(first, deployment, 1))
        self.assertFalse(
            hooks._claim_deployment_concurrency_slot(
                _request("turn-2", deployment_id="route-a"), deployment, 1
            )
        )

        hooks._release_request_concurrency_slots(first)

        self.assertTrue(
            hooks._claim_deployment_concurrency_slot(
                _request("turn-2", deployment_id="route-a"), deployment, 1
            )
        )

    def test_one_turn_holds_one_slot_however_many_times_it_re_enters(self) -> None:
        """A protocol-fallback retry on one deployment is still one request."""

        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        deployment = _deployment("route-a")
        request = _request("turn-1", deployment_id="route-a")

        self.assertTrue(hooks._claim_deployment_concurrency_slot(request, deployment, 1))
        # The same turn asking again is the same request, not a second one.
        self.assertTrue(hooks._claim_deployment_concurrency_slot(request, deployment, 1))
        self.assertEqual(hooks._deployment_in_flight_count("id:route-a"), 1)
        self.assertEqual(len(self.leases()), 1)

    def test_a_route_that_states_zero_never_counts_against_the_limit(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        deployment = _deployment("route-a", max_concurrency=0)

        available, saturated, filtered = hooks._with_active_model_concurrency(
            [deployment],
            request_kwargs=_request("turn-new"),
        )

        self.assertEqual(available, [deployment])
        self.assertEqual(saturated, [])
        self.assertFalse(filtered)

        # Zero is not a cap, so no slot is even claimed for it.
        self.assertTrue(hooks._claim_deployment_concurrency_slot(_request(), deployment, 0))
        self.assertEqual(hooks._deployment_in_flight_count("id:route-a"), 0)


class SharedLeaseFileTests(ConcurrencyStoreTestCase):
    """The file, not this process's memory, is what decides a claim."""

    def test_a_claim_is_visible_in_the_shared_file(self) -> None:
        hooks, _ = load_hook_module()
        deployment = _deployment("route-a")

        hooks._claim_deployment_concurrency_slot(
            _request("turn-1", deployment_id="route-a"), deployment, 4
        )

        leases = self.leases()
        self.assertEqual(len(leases), 1)
        (state,) = leases.values()
        self.assertEqual(state["deployment"], "id:route-a")
        self.assertEqual(state["pid"], os.getpid())

    def test_a_lease_written_by_another_worker_is_counted_here(self) -> None:
        """Simulates a worker this process never talked to: only the file links them."""

        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        deployment = _deployment("route-a")
        foreign = {
            "id:route-a|deadbeef|request": {
                "deployment": "id:route-a",
                "claimed_at": time.time(),
                # A pid that is certainly alive: this test process itself, so
                # the lease is not pruned as an abandoned one.
                "pid": os.getpid(),
            }
        }
        self.leases_path.write_text(
            json.dumps({"schema_version": 1, "leases": foreign}), encoding="utf-8"
        )

        # Memory knows nothing about that lease; the file does, so the cap is
        # already full and this process must refuse rather than take slot two.
        self.assertFalse(
            hooks._claim_deployment_concurrency_slot(_request("turn-a"), deployment, 1)
        )
        self.assertEqual(hooks._deployment_in_flight_count("id:route-a"), 1)
        available, saturated, filtered = hooks._with_active_model_concurrency(
            [deployment], request_kwargs=_request("turn-a")
        )
        self.assertEqual(available, [])
        self.assertEqual([entry["id"] for entry in saturated], ["route-a"])
        self.assertTrue(filtered)

    def test_a_lease_from_a_dead_process_stops_counting(self) -> None:
        hooks, _ = load_hook_module()
        deployment = _deployment("route-a")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.leases_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "leases": {
                        "id:route-a|gone": {
                            "deployment": "id:route-a",
                            "claimed_at": time.time(),
                            "pid": dead.pid,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(hooks._deployment_in_flight_count("id:route-a"), 0)
        self.assertTrue(
            hooks._claim_deployment_concurrency_slot(_request("turn-new"), deployment, 1)
        )

    def test_an_expired_lease_stops_counting(self) -> None:
        hooks, _ = load_hook_module()
        deployment = _deployment("route-a")
        self.leases_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "leases": {
                        "id:route-a|old": {
                            "deployment": "id:route-a",
                            "claimed_at": time.time() - 10 * hooks.lease_seconds(),
                            "pid": os.getpid(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(hooks._deployment_in_flight_count("id:route-a"), 0)
        self.assertTrue(
            hooks._claim_deployment_concurrency_slot(_request("turn-new"), deployment, 1)
        )

    def test_the_proxy_start_drops_every_lease(self) -> None:
        hooks, _ = load_hook_module()
        deployment = _deployment("route-a")
        hooks._claim_deployment_concurrency_slot(_request("turn-1"), deployment, 4)
        self.assertEqual(len(self.leases()), 1)

        hooks.reset_leases()

        self.assertEqual(self.leases(), {})


class CrossProcessConcurrencyTests(unittest.TestCase):
    """Two real processes, one shared cap.

    This is the property the cap exists for: the proxy runs as sixteen uvicorn
    workers, and a count kept per worker let a route capped at 1 serve sixteen
    at once.  The check imports only the store, so it is cheap enough to fork a
    real interpreter and hold the claim from there.
    """

    SCRIPT = textwrap.dedent(
        """
        import sys

        from young_router.proxy import concurrency

        lease_key, deployment_key, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])
        held = concurrency.claim(lease_key, deployment_key, cap)
        print("held" if held else "refused", flush=True)
        # A refused claim is over; a held one must stay owned by this live
        # process, because a dead owner's lease is pruned by design.
        if held:
            sys.stdin.readline()
        """
    )

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "deployment-concurrency.json"
        self.holders: list[subprocess.Popen] = []
        self.addCleanup(self._stop_holders)

    def _stop_holders(self) -> None:
        for holder in self.holders:
            try:
                holder.stdin.close()
            except (OSError, AttributeError):
                pass
            holder.wait(timeout=120)

    def _child_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "YOUNG_ROUTER_DEPLOYMENT_CONCURRENCY_FILE": str(self.path),
            "PYTHONPATH": str(ROOT),
        }

    def hold_in_child(self, lease_key: str, cap: int = 1) -> None:
        holder = subprocess.Popen(
            [sys.executable, "-c", self.SCRIPT, lease_key, "id:route-a", str(cap)],
            cwd=str(ROOT),
            env=self._child_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.holders.append(holder)
        line = holder.stdout.readline().strip()
        self.assertEqual(line, "held", f"worker did not take the slot: {line!r}")

    def claim_in_child(self, lease_key: str, cap: int = 1) -> str:
        # An empty stdin is EOF: a holder exits at once instead of waiting for
        # a line nobody will send, so a regression that takes the slot still
        # reports it rather than hanging the suite.
        result = subprocess.run(
            [sys.executable, "-c", self.SCRIPT, lease_key, "id:route-a", str(cap)],
            cwd=str(ROOT),
            env=self._child_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_one_worker_taking_the_last_slot_makes_the_next_one_wait(self) -> None:
        self.hold_in_child("id:route-a|first|request", cap=1)
        # A different process, a different in-memory world.  Only the file can
        # tell it the slot is gone, and it is what refuses this one.
        self.assertEqual(self.claim_in_child("id:route-a|second|request", cap=1), "refused")

    def test_the_cap_counts_requests_rather_than_workers(self) -> None:
        for index in range(3):
            self.hold_in_child(f"id:route-a|t{index}|request", cap=3)
        self.assertEqual(self.claim_in_child("id:route-a|t3|request", cap=3), "refused")

    def test_a_worker_that_died_does_not_hold_the_slot(self) -> None:
        """A killed worker releases nothing, so its lease has to stop counting."""

        self.hold_in_child("id:route-a|first|request", cap=1)
        self.assertEqual(self.claim_in_child("id:route-a|second|request", cap=1), "refused")

        holder = self.holders.pop()
        holder.kill()
        holder.wait(timeout=120)

        # Left to the lease TTL, a killed worker's slot would hold a route below
        # its cap for the whole request budget.
        self.assertEqual(self.claim_in_child("id:route-a|third|request", cap=1), "held")


class PatchedSelectionConcurrencyTests(ConcurrencyStoreTestCase):
    """The cap is enforced where the route is actually called.

    The candidate filter can only skip a full route while a peer is free.  A
    group whose every route is full is still offered -- a one-route model has
    nowhere else to go -- so the refusal has to exist at the attempt itself,
    which is what these tests reach through the patched selection.
    """

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

    def test_a_full_route_is_refused_at_the_attempt_that_cannot_be_served(self) -> None:
        hooks, Router = self._install()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        deployment = _deployment("route-only")
        held = _request("turn-busy", deployment_id="route-only")
        self.assertTrue(hooks._claim_deployment_concurrency_slot(held, deployment, 1))

        with self.assertRaises(Exception) as caught:
            Router()._update_kwargs_with_deployment(
                deployment, _request("turn-new", deployment_id="route-only")
            )

        self.assertTrue(hooks._is_model_concurrency_rejection_error(caught.exception))
        # The refusal is this proxy's own answer: it must not be reported as an
        # upstream rate limit that a client would back off the model for.
        self.assertIsNone(hooks._exception_status_code(caught.exception))

    def test_a_free_route_is_served_and_holds_its_slot(self) -> None:
        hooks, Router = self._install()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "2")
        deployment = _deployment("route-a")

        Router()._update_kwargs_with_deployment(deployment, _request("turn-1"))

        self.assertEqual(hooks._deployment_in_flight_count("id:route-a"), 1)

    def test_a_delivered_answer_gives_the_slot_back(self) -> None:
        hooks, Router = self._install()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        deployment = _deployment("route-a")
        request = _request("turn-1")
        Router()._update_kwargs_with_deployment(deployment, request)
        self.assertEqual(hooks._deployment_in_flight_count("id:route-a"), 1)

        hooks._record_deployment_success_for_cooldown(request)

        self.assertEqual(hooks._deployment_in_flight_count("id:route-a"), 0)
        # The route is immediately eligible again, which is what makes the cap
        # a concurrency limit rather than a cooldown.
        self.assertTrue(hooks._claim_deployment_concurrency_slot(request, deployment, 1))


class ConcurrencyRefusalHopTests(ConcurrencyStoreTestCase):
    """A refusal on one route becomes the next route's turn."""

    @staticmethod
    def _router(deployments: list[dict]):
        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return list(deployments)

        return Router()

    def test_a_full_route_hands_the_turn_to_its_peer(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        busy = _deployment("route-a")
        peer = _deployment("route-b")
        held = _request("turn-busy", deployment_id="route-a")
        self.assertTrue(hooks._claim_deployment_concurrency_slot(held, busy, 1))

        request = _request("turn-new", deployment_id="route-a")
        error = hooks._model_concurrency_rejection_exception(request, busy)
        hooks._mark_exception_for_deployment_failover(error, request)

        entry = hooks._ordered_deployment_fallback_entry(
            self._router([busy, peer]), error, request
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        # The busy route is excluded and the peer is the one offered, so the
        # turn moves on rather than ending on the route that happened to be busy.
        self.assertIn("route-a", entry["_excluded_deployment_ids"])
        self.assertEqual(entry[hooks._VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY], ["route-b"])

    def test_a_full_group_offers_no_hop_and_keeps_its_own_routes(self) -> None:
        """No peer is invented for a group that has only the busy route."""

        hooks, _ = load_hook_module()
        self.set_env(hooks.MODEL_CONCURRENCY_LIMIT_ENV, "1")
        busy = _deployment("route-a")
        held = _request("turn-busy", deployment_id="route-a")
        self.assertTrue(hooks._claim_deployment_concurrency_slot(held, busy, 1))

        request = _request("turn-new", deployment_id="route-a")
        error = hooks._model_concurrency_rejection_exception(request, busy)
        hooks._mark_exception_for_deployment_failover(error, request)

        entry = hooks._ordered_deployment_fallback_entry(
            self._router([busy]), error, request
        )

        self.assertIsNone(entry)


class ConcurrencyStopSentenceTests(ConcurrencyStoreTestCase):
    """The stop a saturated model produces has to name its own cause.

    The client decides whether to resend from the rendered sentence, so a
    concurrency refusal must read as one: the generic "temporary upstream route
    failure ... stream ended without" tail names an upstream that was never
    asked and a stream that never existed.
    """

    # The slice of Pi's phrase list this router words into its transient stops,
    # mirrored here so a reworded sentence fails loudly instead of quietly
    # ending live turns.
    RETRY_PHRASES = re.compile(
        r"ended without|connection lost|terminated|timed? out|timeout"
        r"|server error|rate.?limit|too many requests|you can retry your request"
        r"|please retry your request",
        re.IGNORECASE,
    )
    # Pi reads a quota, billing, or usage-limit sentence as final however much
    # it looks like advice, so a transient stop must not carry one.
    NON_RETRYABLE = re.compile(
        r"insufficient_quota|out of budget|quota exceeded|billing|available balance"
        r"|GoUsageLimitError|FreeUsageLimitError|Monthly usage limit reached",
        re.IGNORECASE,
    )

    def test_the_stop_names_the_limit_and_asks_for_a_resend(self) -> None:
        hooks, _ = load_hook_module()
        deployment = _deployment("route-only")
        error = hooks._model_concurrency_rejection_exception(_request(), deployment)

        message = hooks._sanitized_upstream_route_failure_message(
            "default-chat", error, _request()
        )

        self.assertIn("concurrency limit", message)
        self.assertIn("already serving as many requests", message)
        # Nothing upstream refused, so the sentence must not blame an upstream
        # route failure or claim a stream ended.
        self.assertNotIn("Temporary upstream route failure", message)
        self.assertNotIn("stream ended without", message)
        self.assertRegex(message, self.RETRY_PHRASES)
        self.assertNotRegex(message, self.NON_RETRYABLE)


class AttemptLeaseIdentityTests(ConcurrencyStoreTestCase):
    """The turn, not the attempt, is what a slot belongs to."""

    async def test_the_pre_call_hook_stamps_one_identifier_per_turn(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        request = {"model": "default-chat", "messages": [{"role": "user", "content": "hi"}]}

        self.assertIsNone(hooks._attempt_lease_id(request))
        # The hook changes nothing else on this request, so it answers None and
        # the identifier it stamped rides on the request the caller already owns.
        self.assertIsNone(
            await hook.async_pre_call_hook({}, None, request, None)
        )
        lease_id = hooks._attempt_lease_id(request)

        self.assertIsInstance(lease_id, str)
        self.assertTrue(lease_id)
        # The marker rides in LiteLLM's own internal bucket, never at the top
        # level: a top-level key is forwarded to the provider as a stray body
        # field, and it is dropped before the success callback that has to give
        # the slot back.
        self.assertNotIn(hooks._ATTEMPT_LEASE_ID_KEY, request)
        self.assertEqual(request["litellm_metadata"][hooks._ATTEMPT_LEASE_ID_KEY], lease_id)
        # A second hook pass over the same turn keeps the identifier it has, so
        # the lease does not move out from under the attempt that holds it.
        await hook.async_pre_call_hook({}, None, request, None)
        self.assertEqual(hooks._attempt_lease_id(request), lease_id)

    def test_the_callback_shape_names_the_same_turn(self) -> None:
        """The claim and the release must compute one key, in either shape.

        A success callback is handed the logging layer's copy of the request, so
        the marker is nested under ``litellm_params``.  If the release looked
        only at the top level it would find nothing and leave the slot behind,
        which is a route that serves less than its cap for the lease window.
        """

        hooks, _ = load_hook_module()
        claim_request = {"litellm_metadata": {hooks._ATTEMPT_LEASE_ID_KEY: "turn-x"}}
        callback_request = {
            "litellm_params": {"litellm_metadata": {hooks._ATTEMPT_LEASE_ID_KEY: "turn-x"}}
        }

        self.assertEqual(hooks._attempt_lease_id(claim_request), "turn-x")
        self.assertEqual(hooks._attempt_lease_id(callback_request), "turn-x")
        # ``litellm_trace_id`` remains a fallback for a path that fills it, and
        # it must not acquire a container suffix the claim never writes.
        self.assertEqual(hooks._attempt_lease_id({"litellm_trace_id": "trace-y"}), "trace-y")

    def test_a_claimed_slot_is_released_from_the_callback_shape(self) -> None:
        """A delivered answer gives back the slot when the callback reports it."""

        hooks, _ = load_hook_module()
        deployment = _deployment("route-a")
        claim_request = _request("turn-cb")
        claim_request["litellm_metadata"] = {hooks._ATTEMPT_LEASE_ID_KEY: "turn-cb"}

        self.assertTrue(
            hooks._claim_deployment_concurrency_slot(claim_request, deployment, 1)
        )
        self.assertEqual(hooks._deployment_in_flight_count("id:route-a"), 1)

        hooks._record_deployment_success_for_cooldown(
            {"litellm_params": {"litellm_metadata": dict(claim_request["litellm_metadata"])}}
        )

        self.assertEqual(hooks._deployment_in_flight_count("id:route-a"), 0)


def test_the_proxy_and_the_config_editor_name_one_key() -> None:
    """Both halves must spell the route's cap the same way.

    The editor writes the key through ``config.schema``, the store reads it off
    the deployment, and the pane states it — so a rename on one side would make
    every route's own number unreachable while the pane kept writing it.
    """

    from young_router.config.schema import MODEL_CONCURRENCY_KEY, MODEL_CONCURRENCY_MAX
    from young_router.proxy import concurrency

    assert concurrency.MODEL_CONCURRENCY_LIMIT_KEY == MODEL_CONCURRENCY_KEY
    assert concurrency.MODEL_CONCURRENCY_MAX == MODEL_CONCURRENCY_MAX


class SharedStoreUnavailableTests(ConcurrencyStoreTestCase):
    """A cap that cannot be shared is reported, never pretended.

    The store is a file under this installation's runtime root.  When that file
    cannot be written the count is unknown, so filtering on it would enforce a
    number no other worker agrees with -- and silently doing nothing would let
    the operator believe a route is capped.  Both halves are stated in the trace
    instead.
    """

    def test_an_unwritable_store_filters_nothing_and_says_so(self) -> None:
        hooks, _ = load_hook_module()
        # A directory where the leases file should be: the lock cannot be taken,
        # so every read of the shared count fails.
        self.leases_path.mkdir(parents=True, exist_ok=True)
        deployment = _deployment("route-a")
        self.assertIsNone(hooks._deployment_in_flight_count("id:route-a"))

        with self.assertLogs("young_router.route_trace", level="WARNING") as captured:
            available, saturated, filtered = hooks._with_active_model_concurrency(
                [deployment], request_kwargs=_request("turn-new")
            )

        self.assertEqual(available, [deployment])
        self.assertEqual(saturated, [])
        self.assertFalse(filtered)
        self.assertTrue(
            any(
                "model_concurrency_store_unavailable" in line
                for line in captured.output
            )
        )


class CallbackShapeLeaseTests(ConcurrencyStoreTestCase):
    """The turn's identifier must survive the trip through LiteLLM.

    These are regression tests for a defect that only appeared on the live
    proxy: the slot was claimed with an identifier nothing could release, so a
    route served less than its cap until the lease expired.  Each case is one
    half of that trip -- the marker's *location* on the way in, and the marker's
    *container* on the way back.
    """

    def test_a_stamped_marker_is_never_sent_to_the_provider(self) -> None:
        hooks, _ = load_hook_module()
        request = {"model": "default-chat", "messages": [{"role": "user", "content": "hi"}]}

        hooks._ensure_attempt_lease_id(request)

        self.assertNotIn(hooks._ATTEMPT_LEASE_ID_KEY, request)
        self.assertIn(hooks._ATTEMPT_LEASE_ID_KEY, request["litellm_metadata"])

    def test_an_existing_container_is_copied_rather_than_mutated(self) -> None:
        hooks, _ = load_hook_module()
        original = {"other": "value"}
        request = {"litellm_metadata": original}

        hooks._ensure_attempt_lease_id(request)

        self.assertEqual(original, {"other": "value"})
        self.assertEqual(request["litellm_metadata"]["other"], "value")
        self.assertIn(hooks._ATTEMPT_LEASE_ID_KEY, request["litellm_metadata"])

    def test_a_marker_wins_over_a_trace_id_whichever_container_holds_it(self) -> None:
        """Key-first lookup: the marker is the turn's own, the trace id is not.

        On the way in the request carries LiteLLM's ``litellm_trace_id`` at the
        top level; on the way back the marker is nested under ``litellm_params``.
        A container-first search answers two different identifiers for one turn,
        which is what left the slot behind.
        """

        hooks, _ = load_hook_module()
        outgoing = {
            "litellm_trace_id": "trace-in",
            "litellm_metadata": {hooks._ATTEMPT_LEASE_ID_KEY: "turn-1"},
        }
        incoming = {
            "litellm_trace_id": "trace-out",
            "litellm_params": {"litellm_metadata": {hooks._ATTEMPT_LEASE_ID_KEY: "turn-1"}},
        }

        self.assertEqual(hooks._attempt_lease_id(outgoing), "turn-1")
        self.assertEqual(hooks._attempt_lease_id(incoming), "turn-1")


class TurnEndReleasesTheSlotTests(ConcurrencyStoreTestCase):
    """A finished turn is not a request in flight.

    The lease only ages out after the whole request budget, so a turn that never
    releases its slot keeps showing as busy for two hours; eight of them park the
    route. Observed live 2026-11-04 on the gateway: two successful turns left two
    leases behind, and two routes of one group read as saturated within minutes.
    """

    async def test_a_successful_turn_gives_its_slot_back(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request = _request(turn="turn-success")
        request["stream"] = False
        self.assertTrue(
            hooks._claim_deployment_concurrency_slot(
                request, _deployment("route-a"), 8
            )
        )
        self.assertEqual(1, len(self.leases()))

        await hooks.YoungRouterHook().async_post_call_success_deployment_hook(
            request_data=request,
            response={"output": []},
            call_type="aresponses",
        )

        self.assertEqual({}, self.leases())

    async def test_a_failed_turn_gives_its_slot_back(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request = _request(turn="turn-failure")
        request["stream"] = False
        self.assertTrue(
            hooks._claim_deployment_concurrency_slot(
                request, _deployment("route-a"), 8
            )
        )

        await hooks.YoungRouterHook().async_log_failure_event(
            kwargs=request,
            response_obj=RuntimeError("upstream refused"),
            start_time=None,
            end_time=None,
        )

        self.assertEqual({}, self.leases())

    async def test_a_streamed_turn_gives_its_slot_back_when_the_reader_leaves(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request = _request(turn="turn-stream")
        self.assertTrue(
            hooks._claim_deployment_concurrency_slot(
                request, _deployment("route-a"), 8
            )
        )

        async def endless_stream():
            yield {"type": "response.created", "response": {"id": "resp-1"}}
            yield {"type": "response.output_text.delta", "delta": "a"}
            await asyncio.sleep(3600)

        stream = hooks.YoungRouterHook().async_post_call_streaming_iterator_hook(
            user_api_key_dict=None,
            response=endless_stream(),
            request_data=request,
        )
        await anext(stream)
        await anext(stream)
        await stream.aclose()

        self.assertEqual({}, self.leases())

    async def test_a_failed_attempt_gives_its_slot_back_before_the_hop(self) -> None:
        """A turn that fails over must not leave the failed route's slot taken.

        This is the shape a refusing route produces on every turn: the attempt
        claims a slot, the route refuses, the turn advances to a sibling. The
        lease stayed behind and the failed route kept counting a request nobody
        was serving, until enough of them parked it.
        """

        hooks, _proxy_server = load_hook_module()
        request = _request(turn="turn-hop")
        self.assertTrue(
            hooks._claim_deployment_concurrency_slot(
                request, _deployment("route-a"), 8
            )
        )
        self.assertEqual(1, len(self.leases()))

        class Refused(Exception):
            status_code = 400

        failure = Refused("thinking mode does not support this tool_choice")
        hooks._mark_exception_for_deployment_failover(failure, request)

        self.assertEqual({}, self.leases())

    def test_the_lease_is_named_by_the_identifier_both_copies_carry(self) -> None:
        """One turn's slot must have one name on the way in and on the way out.

        A marker of this proxy's own making is stripped from some copies of the
        request; when it is, the release falls back to LiteLLM's own id and no
        longer names the lease it has to give back. Measured live 2026-11-04:
        every release named an id no lease carried.
        """

        hooks, _proxy_server = load_hook_module()
        request = _request(turn="ignored")
        request.pop("_young_router_attempt_lease_id")
        request["litellm_trace_id"] = "trace-abc"

        self.assertEqual("trace-abc", hooks._attempt_lease_id(request))
        # The same request after a callback dropped the metadata this proxy
        # stamped still resolves to that one name.
        stripped = {
            "litellm_trace_id": "trace-abc",
            "model_info": {"id": "route-a"},
        }
        self.assertEqual("trace-abc", hooks._attempt_lease_id(stripped))

    def test_a_slot_is_released_under_any_name_the_turn_is_known_by(self) -> None:
        """The claim and the release see different copies of the request.

        One copy keeps this proxy's marker, another keeps only LiteLLM's trace
        id, a third only the client's own request id. A slot registered under one
        name and looked for under another is never given back, which is what made
        every turn leak the slot it had just claimed.
        """

        hooks, _proxy_server = load_hook_module()
        claim_copy = {
            "_young_router_attempt_lease_id": "lease-1",
            "model_info": {"id": "route-a"},
        }
        release_copy = {
            "litellm_trace_id": "trace-1",
            "model_info": {"id": "route-a"},
        }

        self.assertTrue(
            hooks._claim_deployment_concurrency_slot(
                claim_copy, _deployment("route-a"), 8
            )
        )
        # Both copies name the same turn by its marker, so the release finds it.
        self.assertEqual(
            ["lease-1"], hooks._lease_identifier_candidates(claim_copy)
        )

        hooks._HELD_CONCURRENCY_LEASES.setdefault(
            "trace-1", set()
        ).add(sorted(hooks._HELD_CONCURRENCY_LEASES["lease-1"])[0])
        hooks._release_request_concurrency_slots(release_copy)

        self.assertEqual({}, self.leases())
        self.assertEqual({}, dict(hooks._HELD_CONCURRENCY_LEASES))
