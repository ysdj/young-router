"""Edge cases of the shared deployment concurrency store.

The queue names turns, the leases name turns on routes.  These tests keep the
two key spaces apart: a ticket is a turn's place in one route's line, not a
claim that the turn already holds a slot there.
"""

from __future__ import annotations

from hook_test_utils import *


class ConcurrencyQueueTestCase(HookTestCase):
    """Runs the store against a private leases file, as the hook module does."""

    def setUp(self) -> None:
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.leases_path = Path(self.directory.name) / "deployment-concurrency.json"
        self.set_env(
            "YOUNG_ROUTER_DEPLOYMENT_CONCURRENCY_FILE", str(self.leases_path)
        )

    def test_a_ticket_that_looks_like_a_lease_key_still_joins_the_line(self) -> None:
        hooks, _ = load_hook_module()
        concurrency = hooks._concurrency_module
        lease_key = "id:route-a|turn-a"

        self.assertTrue(concurrency.claim(lease_key, "id:route-a", 1))
        self.assertEqual(concurrency.waiting_count("id:route-a"), 0)

        # A lease key names a slot ("route|turn"); a ticket names a turn.  A
        # queue key that happens to look like the former is still a place in
        # this route's line, or the turn would wait without ever being seen.
        self.assertTrue(concurrency.queue(lease_key, "id:route-a", time.time()))
        self.assertEqual(concurrency.waiting_count("id:route-a"), 1)


class NonStreamingAdmissionTests(ConcurrencyQueueTestCase):
    """A non-streaming turn is admitted, not queued.

    The cap is a streaming answer: the waiting turn keeps its stream open with
    keepalives.  A non-streaming caller has no such window and no way to answer
    a refusal except by retrying, so the filter leaves its pool untouched and
    the route is asked for the answer it can give.
    """

    def _pool(self) -> list[dict]:
        return [
            {
                "model_name": "default-chat",
                "litellm_params": {"model": "openai/fast-chat"},
                "model_info": {"id": "deploy-a", "max_concurrency": 1},
            }
        ]

    def test_a_full_route_still_serves_a_non_streaming_turn(self) -> None:
        hooks, _ = load_hook_module()
        concurrency = hooks._concurrency_module
        pool = self._pool()
        lease_key = f"{hooks._routing_module._deployment_cooldown_key_from_deployment(pool[0])}|turn-a"
        self.assertTrue(concurrency.claim(lease_key, "id:deploy-a", 1))

        available, saturated, filtered = hooks._routing_module._with_active_model_concurrency(
            pool, request_kwargs={"model": "default-chat", "messages": []}
        )

        self.assertEqual(pool, available)
        self.assertEqual([], saturated)
        self.assertFalse(filtered)

    def test_a_full_route_holds_a_streaming_turn_back(self) -> None:
        hooks, _ = load_hook_module()
        concurrency = hooks._concurrency_module
        pool = self._pool()
        lease_key = f"{hooks._routing_module._deployment_cooldown_key_from_deployment(pool[0])}|turn-b"
        self.assertTrue(concurrency.claim(lease_key, "id:deploy-a", 1))

        available, saturated, filtered = hooks._routing_module._with_active_model_concurrency(
            pool, request_kwargs={"model": "default-chat", "stream": True}
        )

        self.assertEqual([], available)
        self.assertEqual(1, len(saturated))
        self.assertTrue(filtered)


if __name__ == "__main__":
    unittest.main()