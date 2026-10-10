"""Core contracts for the public-model pane: rename, limits, route order."""

from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import unittest

from young_router.core.domains import DomainError
from young_router.core.domains.providers_models import ProvidersModelsDomain


PUBLIC_MODEL_CONFIG = """
providers:
  primary:
    api_base: "https://example.test/v1"
    api_keys:
      - name: default
        value: "replace-me-secret"
  backup:
    api_base: "https://backup.test/v1"
    api_keys:
      - name: default
        value: "replace-me-secret-2"
model_list:
  - model_name: gpt-5.6-sol
    litellm_params:
      model: openai/gpt-5.6-sol
      api_base: "https://example.test/v1"
      api_key: "replace-me-secret"
      order: 1
    model_info:
      id: "00000001"
      provider: primary
  - model_name: gpt-5.6-sol
    litellm_params:
      model: openai/gpt-5.6-sol
      api_base: "https://backup.test/v1"
      api_key: "replace-me-secret-2"
      order: 1.5
    model_info:
      id: "00000002"
      provider: backup
  - model_name: kimi-k3
    litellm_params:
      model: openai/kimi-k3
      api_base: "https://example.test/v1"
      api_key: "replace-me-secret"
      order: 2
    model_info:
      id: "00000003"
      provider: primary
"""


class PublicModelDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.yaml"
        self.path.write_text(
            textwrap.dedent(PUBLIC_MODEL_CONFIG).lstrip(), encoding="utf-8"
        )
        self.domain = ProvidersModelsDomain(self.path)

    def models(self) -> list[dict]:
        snapshot = self.domain.snapshot()
        return [
            model
            for provider in snapshot["providers"]
            for model in provider["models"]
        ]

    def model(self, deployment_id: str) -> dict:
        return next(
            model
            for model in self.models()
            if model["deployment_id"] == deployment_id
        )

    def test_a_new_provider_and_model_default_to_enabled(self) -> None:
        """A create that states no enabled flag is live, not parked.

        The panes rely on this: ＋新建供应商 / ＋新建模型 send the new row
        without an enabled flag (or with `enabled: true`), and a create that
        came out disabled would need a second press before the row could serve.
        """

        created = self.domain.dispatch(
            "provider.add",
            {"provider": {"name": "Fresh Provider", "models": []}},
        )
        provider = next(
            entry for entry in created["providers"] if entry["name"] == "Fresh Provider"
        )
        self.assertTrue(provider["enabled"])
        provider_id = str(provider["id"])

        added = self.domain.dispatch(
            "model.add",
            {
                "provider_id": provider_id,
                "model": {"name": "fresh-model", "upstream_model": "fresh-model", "order": 0},
            },
        )
        model = next(
            entry
            for entry in added["providers"][-1]["models"]
            if entry["model_name"] == "fresh-model"
        )
        self.assertTrue(model["enabled"])
        self.assertTrue(model["model_enabled"])
        # An explicitly parked row stays parked.
        parked = self.domain.dispatch(
            "model.add",
            {
                "provider_id": provider_id,
                "model": {
                    "name": "parked-model",
                    "upstream_model": "parked-model",
                    "enabled": False,
                    "order": 1,
                },
            },
        )
        parked_model = next(
            entry
            for entry in parked["providers"][-1]["models"]
            if entry["model_name"] == "parked-model"
        )
        self.assertFalse(parked_model["enabled"])

    def test_public_model_patch_writes_the_context_of_every_group_route(self) -> None:
        snapshot = self.domain.dispatch(
            "public.model_patch",
            {
                "public_model": "gpt-5.6-sol",
                "changes": {"max_input_tokens": 372000},
            },
        )
        routes = [
            model
            for provider in snapshot["providers"]
            for model in provider["models"]
            if model["model_name"] == "gpt-5.6-sol"
        ]
        self.assertEqual(2, len(routes))
        for route in routes:
            self.assertEqual(372000, route["max_input_tokens"])
        self.assertIsNone(self.model("00000003")["max_input_tokens"])

        self.domain.apply()
        saved = self.path.read_text(encoding="utf-8")
        self.assertEqual(2, saved.count("max_input_tokens: 372000"))

        reloaded = ProvidersModelsDomain(self.path)
        self.assertEqual(372000, self.model("00000001")["max_input_tokens"])
        self.assertEqual(372000, reloaded.snapshot()["providers"][0]["models"][0]["max_input_tokens"])

    def test_clearing_the_context_removes_the_key_instead_of_writing_zero(self) -> None:
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"max_input_tokens": 372000}},
        )
        self.domain.apply()
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"max_input_tokens": None}},
        )
        self.domain.apply()
        saved = self.path.read_text(encoding="utf-8")
        self.assertNotIn("max_input_tokens", saved)
        self.assertIsNone(self.model("00000001")["max_input_tokens"])

    def test_public_model_rename_rewrites_every_route_of_the_group(self) -> None:
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"name": "sol-fast"}},
        )
        self.domain.apply()
        saved = self.path.read_text(encoding="utf-8")
        self.assertEqual(2, saved.count("model_name: sol-fast"))
        self.assertNotIn("model_name: gpt-5.6-sol", saved)
        # The upstream route stays the provider's own model id.
        self.assertEqual(2, saved.count("model: openai/gpt-5.6-sol"))
        self.assertEqual("kimi-k3", self.model("00000003")["model_name"])

    def test_rename_refuses_a_name_another_public_model_already_uses(self) -> None:
        with self.assertRaises(DomainError):
            self.domain.dispatch(
                "public.model_patch",
                {"public_model": "gpt-5.6-sol", "changes": {"name": "kimi-k3"}},
            )

    def test_public_model_patch_refuses_unknown_changes(self) -> None:
        with self.assertRaises(DomainError):
            self.domain.dispatch(
                "public.model_patch",
                {"public_model": "gpt-5.6-sol", "changes": {"provider": "backup"}},
            )

    def test_a_route_refuses_a_context_window_of_its_own(self) -> None:
        """The Codex window belongs to the public model, not to one route.

        ``public.model_patch`` writes the number to every route of the group, so
        a route-level write is what would make one route disagree with the
        others.  It is refused with a sentence that names where the write
        belongs, instead of silently landing on a single route.
        """

        with self.assertRaisesRegex(DomainError, "public model, not on one route"):
            self.domain.dispatch(
                "model.patch",
                {
                    "provider_id": "primary",
                    "model_id": "00000001",
                    "changes": {"max_input_tokens": 200000},
                },
            )
        self.assertIsNone(self.model("00000001")["max_input_tokens"])
        self.assertIsNone(self.model("00000002")["max_input_tokens"])

        # The group's own action still writes it, to every route at once.
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"max_input_tokens": 200000}},
        )
        self.assertEqual(200000, self.model("00000001")["max_input_tokens"])
        self.assertEqual(200000, self.model("00000002")["max_input_tokens"])

    def test_a_negative_context_is_refused(self) -> None:
        with self.assertRaises(DomainError):
            self.domain.dispatch(
                "public.model_patch",
                {"public_model": "gpt-5.6-sol", "changes": {"max_input_tokens": -4}},
            )

    def test_deleting_a_public_model_removes_every_route_that_serves_it(self) -> None:
        """One name is one group: deleting it deletes its routes, not one of them.

        The routes live on two providers (and a parked route lives in the
        companion file), so the action has to reach all of them in one staged
        change — a partial group would leave the name served by a route the user
        believes is gone.
        """

        # A third route of the same public model, parked in the companion file.
        self.domain.dispatch(
            "model.add",
            {
                "provider_id": "primary",
                "model": {
                    "name": "gpt-5.6-sol",
                    "upstream_model": "openai/gpt-5.6-sol",
                    "enabled": False,
                    "order": 3,
                },
            },
        )
        self.domain.apply()
        before = [model["model_name"] for model in self.models()]
        self.assertEqual(4, len(before))

        result = self.domain.dispatch("public.model_delete", {"public_model": "gpt-5.6-sol"})

        self.assertEqual(
            {
                "operation": "public_model_delete",
                "public_model": "gpt-5.6-sol",
                "deleted_models": 3,
            },
            result["operation_summary"],
        )
        self.assertEqual(["kimi-k3"], [model["model_name"] for model in self.models()])
        self.assertTrue(self.domain.validate()["valid"])

        self.domain.apply()
        reloaded = ProvidersModelsDomain(self.path)
        self.assertEqual(["kimi-k3"], [model["model_name"] for model in reloaded.snapshot()["providers"][0]["models"] + reloaded.snapshot()["providers"][1]["models"]])
        self.assertNotIn("gpt-5.6-sol", self.path.read_text(encoding="utf-8"))

        # A name no route serves is refused instead of silently doing nothing.
        with self.assertRaises(DomainError):
            self.domain.dispatch("public.model_delete", {"public_model": "gpt-5.6-sol"})
        with self.assertRaises(DomainError):
            self.domain.dispatch("public.model_delete", {"public_model": ""})

    def test_reorder_permutes_the_typed_order_values_including_decimals(self) -> None:
        self.domain.dispatch(
            "routes.reorder_group",
            {
                "public_model": "gpt-5.6-sol",
                "route_ids": ["00000002", "00000001"],
            },
        )
        # The two routes trade the values the user typed (1 and 1.5) instead
        # of being renumbered to whole numbers.
        self.assertEqual(1, self.model("00000002")["manual_order"])
        self.assertEqual(1.5, self.model("00000001")["manual_order"])

    def test_reorder_answers_the_identity_the_routes_table_holds(self) -> None:
        """The 路由 table sends the editor identity its rows carry.

        A route has two names — the deployment id the file persists and the
        editor id the snapshot carries — and the pane's own row prefers the
        editor id.  Reordering used to accept only the deployment id, so every
        ↑/↓ in a saved group was refused and the pane voided the answer.
        """

        rows = [
            model
            for provider in self.domain.snapshot()["providers"]
            for model in provider["models"]
            if model["model_name"] == "gpt-5.6-sol"
        ]
        editor_ids = [str(model["editor_id"]).strip() for model in rows]
        self.assertEqual(2, len(set(editor_ids)))
        for model in rows:
            self.assertNotEqual(
                model["editor_id"],
                model["deployment_id"],
                "the two identities must stay distinguishable for this guard",
            )

        self.domain.dispatch(
            "routes.reorder_group",
            {"public_model": "gpt-5.6-sol", "route_ids": list(reversed(editor_ids))},
        )

        self.assertEqual(1, self.model("00000002")["manual_order"])
        self.assertEqual(1.5, self.model("00000001")["manual_order"])

        # An identity the group does not carry is still a stale order, and a
        # request that names fewer routes than the group has is refused too.
        with self.assertRaises(DomainError):
            self.domain.dispatch(
                "routes.reorder_group",
                {"public_model": "gpt-5.6-sol", "route_ids": ["model-does-not-exist", "00000002"]},
            )
        with self.assertRaises(DomainError):
            self.domain.dispatch(
                "routes.reorder_group",
                {"public_model": "gpt-5.6-sol", "route_ids": [editor_ids[0]]},
            )

    def test_reorder_renumbers_to_integers_when_the_user_asks_for_it(self) -> None:
        """The pane asks before it replaces a group's typed decimal values.

        Keeping the values is the default (they travel with the routes); the
        answer "make them integers" has to replace the whole group with 1..n in
        the requested order, including the routes parked in the companion file.
        """

        rows = [
            model
            for provider in self.domain.snapshot()["providers"]
            for model in provider["models"]
            if model["model_name"] == "gpt-5.6-sol"
        ]
        order = [str(model["deployment_id"]).strip() for model in rows]

        self.domain.dispatch(
            "routes.reorder_group",
            {"public_model": "gpt-5.6-sol", "route_ids": list(reversed(order)), "renumber": True},
        )

        # The group is 0..n-1 in the new order: the requested first route is 0.
        self.assertEqual(0, self.model(order[1])["manual_order"])
        self.assertEqual(1, self.model(order[0])["manual_order"])
        for deployment_id in order:
            self.assertIsInstance(self.model(deployment_id)["manual_order"], int)

    def test_a_public_name_answers_only_while_one_route_carries_it(self) -> None:
        """The identities a document persists decide their own row.

        A public name is the weaker identity: it answers only while exactly one
        route in that provider carries it (see the same-name-twice case below),
        and it never turns into "the first one".
        """

        # The persisted identities each decide their own row.
        patched = self.domain.dispatch(
            "model.patch",
            {"provider_id": "primary", "model_id": "00000001", "changes": {"model_enabled": False}},
        )
        self.assertFalse(self.model("00000001")["model_enabled"])
        editor_id = next(
            str(model["editor_id"])
            for model in patched["providers"][0]["models"]
            if model["deployment_id"] == "00000001"
        )
        self.domain.dispatch(
            "model.patch",
            {"provider_id": "primary", "model_id": editor_id, "changes": {"model_enabled": True}},
        )
        self.assertTrue(self.model("00000001")["model_enabled"])

        # A name only one route carries is still a usable selector.
        self.domain.dispatch(
            "model.patch",
            {"provider_id": "primary", "model_id": "kimi-k3", "changes": {"model_enabled": False}},
        )
        self.assertFalse(self.model("00000003")["model_enabled"])

        # A name this provider does not carry stays unavailable.
        with self.assertRaisesRegex(DomainError, "selected model is unavailable"):
            self.domain.dispatch(
                "model.patch",
                {"provider_id": "primary", "model_id": "no-such-model", "changes": {"model_enabled": False}},
            )

    def test_one_provider_serving_a_name_twice_refuses_a_name_selector(self) -> None:
        """One provider can serve one public name twice; the name is then ambiguous."""

        added = self.domain.dispatch(
            "model.add",
            {
                "provider_id": "primary",
                "model": {
                    "name": "gpt-5.6-sol",
                    "upstream_model": "openai/gpt-5.6-sol-secondary",
                    "enabled": True,
                    "order": 3,
                },
            },
        )
        second_id = str(next(
            model["editor_id"]
            for provider in added["providers"]
            if str(provider["name"]) == "primary"
            for model in provider["models"]
            if "secondary" in str(model.get("litellm_model"))
        ))

        with self.assertRaisesRegex(DomainError, "Several routes serve this public model"):
            self.domain.dispatch(
                "model.patch",
                {"provider_id": "primary", "model_id": "gpt-5.6-sol", "changes": {"model_enabled": False}},
            )
        # The new route's own id still answers.
        self.domain.dispatch(
            "model.patch",
            {"provider_id": "primary", "model_id": second_id, "changes": {"model_enabled": False}},
        )
        self.assertFalse(
            next(
                model["model_enabled"]
                for provider in self.domain.snapshot()["providers"]
                for model in provider["models"]
                if model["editor_id"] == second_id
            )
        )

    def test_snapshot_carries_the_registry_default_for_each_public_model(self) -> None:
        contexts = self.domain.snapshot()["model_contexts"]
        self.assertEqual(272000, contexts["gpt-5.6-sol"]["context_window"])
        self.assertEqual(262144, contexts["kimi-k3"]["context_window"])

    def test_the_draft_custom_limit_never_masks_the_registry_default(self) -> None:
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"max_input_tokens": 372000}},
        )
        # The pane reads the custom value from its model rows and keeps the
        # registry default beside it as the fallback hint.
        self.assertEqual(
            272000,
            self.domain.snapshot()["model_contexts"]["gpt-5.6-sol"]["context_window"],
        )

    def test_the_pane_receives_the_configured_context_window_verbatim(self) -> None:
        """The snapshot a window reads carries the window's number, not a marker.

        A route's ``max_input_tokens`` is the value the Codex catalog hands the
        client.  Core projects its snapshots through the secret redactor, and a
        bare ``token`` marker once classified that number as a credential: the
        key arrived as the string ``configured``, so the pane's Codex 上下文
        field painted empty, read as "no custom window", and the next commit
        wrote whatever was typed over the window the user had set.
        """

        from young_router.core.service import _safe_public

        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"max_input_tokens": 372000}},
        )
        projected = _safe_public(self.domain.snapshot())
        routes = [
            model
            for provider in projected["providers"]
            for model in provider["models"]
            if model["model_name"] == "gpt-5.6-sol"
        ]
        self.assertEqual(2, len(routes))
        for route in routes:
            self.assertEqual(372000, route["max_input_tokens"])
        # The route that declares no window still reports none, so the pane
        # keeps showing the registry default rather than a custom claim.
        self.assertIsNone(self.model("00000003")["max_input_tokens"])


if __name__ == "__main__":
    unittest.main()
