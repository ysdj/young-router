from __future__ import annotations

from hook_test_utils import *


class IntegerOrderTierTests(HookTestCase):
    """A relay multiplier must reach the router as an integer tier.

    LiteLLM compares ``order`` as an integer and treats a non-integral float as
    "no order".  With every cheap route on a multiplier (0.09 ... 0.5) and one
    manual route at 100, the only ordered route was the last one, so every
    request was pinned to it -- live 2026-11-18: 53 of 69 requests on the
    ``order: 100`` line while the cheap lines idled.
    """

    def test_many_decimal_places_keep_their_order(self) -> None:
        hooks, _ = load_hook_module()
        hooks._install_integer_order_tiers_patch()

        routes = hooks._with_integer_order_tiers(
            [
                self._deployment("cheap", 0.091),
                self._deployment("cheaper", 0.0901),
                self._deployment("cheapest", 0.09),
                self._deployment("mid", 0.15),
                self._deployment("last", 100),
            ]
        )

        # Ranked, so precision is not lost to a fixed number of decimal places.
        self.assertEqual(
            [2, 1, 0, 3, 4],
            [route["litellm_params"]["order"] for route in routes],
        )
        self.assertEqual(0.09, routes[2]["model_info"]["order"])
        # The router's tier filter then keeps the single cheapest tier.
        self.assertEqual(
            ["cheapest"],
            [
                route["model_info"]["id"]
                for route in sorted(routes, key=lambda item: item["litellm_params"]["order"])[:1]
            ],
        )

    def test_a_manual_order_keeps_its_number_beside_the_rank(self) -> None:
        hooks, _ = load_hook_module()
        hooks._install_integer_order_tiers_patch()

        route = self._deployment("manual", 50)
        hooks._with_integer_order_tiers([route])

        # One route, one tier; the number the user set stays readable.
        self.assertEqual(0, route["litellm_params"]["order"])
        self.assertEqual(50, route["model_info"]["order"])

    @staticmethod
    def _deployment(deployment_id: str, order) -> dict:
        return {
            "model_name": "default-chat",
            "litellm_params": {"model": "openai/vendor-chat", "order": order},
            "model_info": {"id": deployment_id},
        }
