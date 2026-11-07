"""Core contracts for the public-model pane: rename, limits, route order."""

from __future__ import annotations

from pathlib import Path
import json
import tempfile
import textwrap
import unittest

from young_router.core.domains import DomainError
from young_router.core.domains.providers_models import ProvidersModelsDomain
from collections.abc import Mapping

from young_router.core.model_catalog import catalog_payload
from young_router.core.model_contexts import ModelContextRegistry


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
  - model_name: MiniMax-M3.1-Flash
    litellm_params:
      model: openai/MiniMax-M3.1-Flash
      api_base: "https://backup.test/v1"
      api_key: "replace-me-secret-2"
      order: 3
    model_info:
      id: "00000004"
      provider: backup
  - model_name: minimax-m3.1-flash
    litellm_params:
      model: openai/minimax-m3.1-flash
      api_base: "https://example.test/v1"
      api_key: "replace-me-secret"
      order: 0.1
    model_info:
      id: "00000005"
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
        self.assertEqual(6, len(before))

        result = self.domain.dispatch("public.model_delete", {"public_model": "gpt-5.6-sol"})

        self.assertEqual(
            {
                "operation": "public_model_delete",
                "public_model": "gpt-5.6-sol",
                "deleted_models": 3,
            },
            result["operation_summary"],
        )
        self.assertEqual(["kimi-k3", "minimax-m3.1-flash", "MiniMax-M3.1-Flash"], [model["model_name"] for model in self.models()])
        self.assertTrue(self.domain.validate()["valid"])

        self.domain.apply()
        reloaded = ProvidersModelsDomain(self.path)
        remaining = [
            model["model_name"]
            for provider in reloaded.snapshot()["providers"]
            for model in provider["models"]
        ]
        self.assertEqual(["kimi-k3", "minimax-m3.1-flash", "MiniMax-M3.1-Flash"], remaining)
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

    def test_two_spellings_of_one_name_are_two_public_models(self) -> None:
        """A name is the group: two spellings are two public models.

        ``minimax-m3.1-flash`` and ``MiniMax-M3.1-Flash`` are two deployment sets
        in the Router, so the pane draws two groups and every action reaches the
        group whose name it states.  The case is settled once, when a model is
        imported; after that nothing folds one spelling onto another behind the
        user's back.
        """

        # One group's action reaches the routes that state its own name.
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "MiniMax-M3.1-Flash", "changes": {"max_input_tokens": 196608}},
        )
        self.assertEqual(196608, self.model("00000004")["max_input_tokens"])
        self.assertIsNone(self.model("00000005")["max_input_tokens"])

        # The projection publishes one entry per name, not one per folded case.
        contexts = self.domain.snapshot()["model_contexts"]
        self.assertIn("MiniMax-M3.1-Flash", contexts)
        self.assertIn("minimax-m3.1-flash", contexts)

        # A reorder names the routes of that one group.
        self.domain.dispatch(
            "routes.reorder_group",
            {
                "public_model": "minimax-m3.1-flash",
                "route_ids": ["00000005"],
                "renumber": True,
            },
        )
        self.assertEqual(0, self.model("00000005")["manual_order"])
        self.assertEqual(3, self.model("00000004")["manual_order"])

        # A rename is the user's spelling and stays exactly as they wrote it.
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "MiniMax-M3.1-Flash", "changes": {"name": "MiniMax-M3.2-Flash"}},
        )
        self.assertEqual("MiniMax-M3.2-Flash", self.model("00000004")["model_name"])
        self.assertEqual("minimax-m3.1-flash", self.model("00000005")["model_name"])
        self.domain.apply()
        saved = self._saved()
        self.assertEqual(
            ["gpt-5.6-sol", "kimi-k3", "minimax-m3.1-flash", "MiniMax-M3.2-Flash"],
            saved["litellm_settings"]["public_model_groups"],
        )

    def test_an_import_files_the_name_it_read_in_lower_case(self) -> None:
        """A catalog label is a label until the import states its spelling.

        A pick from a fetched listing arrives with the name the chooser showed,
        and the question the pane asked is the answer Core files it under: lower
        case by default, the catalog's own spelling when the user kept it.  The
        upstream keeps exactly what the station states either way, and a row the
        user typed is not an import and keeps its case.
        """

        added = self.domain.dispatch(
            "model.add_many",
            {
                "provider_id": "primary",
                "models": [
                    {
                        "name": "MiniMax-M3.2-Flash",
                        "upstream_model": "MiniMax-M3.2-Flash",
                        "imported": True,
                        "enabled": True,
                        "order": 0,
                    }
                ],
            },
        )
        imported = next(
            model
            for model in added["providers"][0]["models"]
            if model["upstream_model"] == "MiniMax-M3.2-Flash"
        )
        self.assertEqual("minimax-m3.2-flash", imported["model_name"])
        self.assertEqual("openai/MiniMax-M3.2-Flash", imported["litellm_model"])

        # The same pick, with the user keeping the catalog's own spelling.
        kept = self.domain.dispatch(
            "model.add_many",
            {
                "provider_id": "primary",
                "models": [
                    {
                        "name": "MiniMax-M3.3-Flash",
                        "upstream_model": "MiniMax-M3.3-Flash",
                        "imported": True,
                        "imported_name_case": "original",
                        "enabled": True,
                        "order": 0,
                    }
                ],
            },
        )
        original = next(
            model
            for model in kept["providers"][0]["models"]
            if model["upstream_model"] == "MiniMax-M3.3-Flash"
        )
        self.assertEqual("MiniMax-M3.3-Flash", original["model_name"])
        self.assertEqual("openai/MiniMax-M3.3-Flash", original["litellm_model"])

        # A decision this app does not issue is not a spelling: unstated, and
        # anything but 保留原样, files lower case.
        fallback = self.domain.dispatch(
            "model.add_many",
            {
                "provider_id": "primary",
                "models": [
                    {
                        "name": "MiniMax-M3.4-Flash",
                        "upstream_model": "MiniMax-M3.4-Flash",
                        "imported": True,
                        "imported_name_case": "KEEP-THE-LABEL",
                        "enabled": True,
                        "order": 0,
                    }
                ],
            },
        )
        self.assertEqual(
            "minimax-m3.4-flash",
            next(
                model["model_name"]
                for model in fallback["providers"][0]["models"]
                if model["upstream_model"] == "MiniMax-M3.4-Flash"
            ),
        )

        typed = self.domain.dispatch(
            "model.add",
            {
                "provider_id": "primary",
                "model": {"name": "My-Model", "upstream_model": "my-model", "order": 0},
            },
        )
        self.assertEqual(
            "My-Model",
            next(
                model["model_name"]
                for model in typed["providers"][0]["models"]
                if model["upstream_model"] == "my-model"
            ),
        )
        # The import's own flags never reach the document they filed.
        for provider in self.domain.draft_state()["providers"]:
            for model in provider.get("models", []):
                self.assertNotIn("imported", model)
                self.assertNotIn("imported_name_case", model)

    def test_a_rename_refuses_a_name_another_group_already_has(self) -> None:
        with self.assertRaisesRegex(DomainError, "already exists"):
            self.domain.dispatch(
                "public.model_patch",
                {"public_model": "gpt-5.6-sol", "changes": {"name": "kimi-k3"}},
            )
        # Another case is another name, and the user is free to state it.
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "kimi-k3", "changes": {"name": "Kimi-K3"}},
        )
        self.assertEqual("Kimi-K3", self.model("00000003")["model_name"])

    def test_the_saved_group_table_lists_each_served_name_once(self) -> None:
        """The group table is the names the document serves, each exactly once.

        Both spellings the fixture states are served by their own rows, so the
        table names both: a client that asks for either case reaches the rows that
        state it, and the table promises nothing else.
        """

        self.domain.apply()
        saved = self._saved()
        self.assertEqual(
            ["gpt-5.6-sol", "kimi-k3", "minimax-m3.1-flash", "MiniMax-M3.1-Flash"],
            saved["litellm_settings"]["public_model_groups"],
        )
        self.assertIn("model_name: MiniMax-M3.1-Flash", self.path.read_text(encoding="utf-8"))

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

    def test_a_public_model_added_after_a_refresh_reads_the_refreshed_cache(self) -> None:
        """The pane resolves against the cache the refresh has already replaced.

        The pane's registry never refreshes; the Codex domain's registry owns
        the refresh and rewrites the one cache file.  A reading taken before
        that write kept answering the unknown-model window for every model the
        refresh is what taught this installation about -- so a model added
        right afterwards was drawn as 272,000 while pi.dev named its profile.
        """

        import json

        def profile() -> dict:
            return {
                "context_window": 1_048_576,
                "max_context_window": 1_048_576,
                "effective_context_window_percent": 95,
                "source": "https://pi.dev/api/models",
                "priority": 40,
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".litellm-runtime"
            runtime.mkdir()
            path = runtime / "config.yaml"
            # A ``model_list`` entry is rejected outright without a
            # ``model_info`` object, so the load fails before this test can
            # reach the cache it is here to read.
            path.write_text(
                "model_list:\n"
                "  - model_name: gemini-4-argon\n"
                "    litellm_params:\n"
                "      model: openai/gemini-4-argon\n"
                "    model_info: {}\n",
                encoding="utf-8",
            )
            # The installation's cache is resolved from the root the document
            # belongs to, and ``.litellm-runtime`` is that root's child.
            cache = root / "model-contexts.json"
            cache.write_text(json.dumps({"fetched_at": 1_000_000.0, "records": {}}), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            before = domain.snapshot()["model_contexts"]["gemini-4-argon"]

            # The Codex domain's registry publishes the profile while this
            # domain is still live, exactly as a refresh runs beside an open pane.
            cache.write_text(
                json.dumps(
                    {
                        "fetched_at": 1_000_060.0,
                        "records": {"opencode/gemini-4-argon": profile()},
                    }
                ),
                encoding="utf-8",
            )
            after = domain.snapshot()["model_contexts"]["gemini-4-argon"]

        self.assertEqual(272000, before["context_window"])
        self.assertEqual(1_048_576, after["context_window"])
        self.assertEqual(1_048_576, after["max_context_window"])

    def _saved(self) -> dict:
        import yaml

        return yaml.safe_load(self.path.read_text(encoding="utf-8"))

    def _with_router_settings(self, block: str) -> None:
        self.path.write_text(
            textwrap.dedent(PUBLIC_MODEL_CONFIG).lstrip() + textwrap.dedent(block),
            encoding="utf-8",
        )
        self.domain = ProvidersModelsDomain(self.path)

    def test_a_rename_restates_the_group_every_router_table_names(self) -> None:
        """Renaming a public model moves the fallback graph with it.

        A public model is the client-facing group name, and `router_settings`
        names groups in its own tables.  LiteLLM resolves those per request, so a
        rename that left them alone pointed every fallback hop at a group with no
        deployments: the Router took the hop, found nothing, and the client got a
        500 reading `Fallback to <gone> also failed` over the real upstream error.
        """

        self._with_router_settings(
            """
            router_settings:
              fallbacks:
                - kimi-k3:
                    - gpt-5.6-sol
              context_window_fallbacks:
                - kimi-k3:
                    - gpt-5.6-sol
              model_group_alias:
                fast-sol: gpt-5.6-sol
            """
        )

        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"name": "sol-next"}},
        )
        self.domain.apply()
        saved = self._saved()

        self.assertEqual(
            [{"kimi-k3": ["sol-next"]}], saved["router_settings"]["fallbacks"]
        )
        self.assertEqual(
            [{"kimi-k3": ["sol-next"]}],
            saved["router_settings"]["context_window_fallbacks"],
        )
        self.assertEqual(
            {"fast-sol": "sol-next"}, saved["router_settings"]["model_group_alias"]
        )
        # Every table it names is a name the document actually serves.
        served = set(saved["litellm_settings"]["public_model_groups"])
        for entry in saved["router_settings"]["fallbacks"]:
            for source, targets in entry.items():
                self.assertIn(source, served)
                for target in targets:
                    self.assertIn(target, served)

    def test_a_delete_drops_the_hop_that_named_only_it(self) -> None:
        """A removed group's fallback hop goes with it, and the save says so."""

        self._with_router_settings(
            """
            router_settings:
              fallbacks:
                - kimi-k3:
                    - gpt-5.6-sol
                    - kimi-k3
              model_group_alias:
                fast-sol: gpt-5.6-sol
            """
        )

        self.domain.dispatch("public.model_delete", {"public_model": "gpt-5.6-sol"})
        self.domain.apply()
        saved = self._saved()

        # Only the removed name is dropped; the surviving sibling keeps its hop.
        self.assertEqual([{"kimi-k3": ["kimi-k3"]}], saved["router_settings"]["fallbacks"])
        self.assertEqual({}, saved["router_settings"]["model_group_alias"])

    def test_a_hop_the_document_never_served_is_reported_not_kept(self) -> None:
        """A reference to a name no route serves is dropped and named.

        It cannot be resolved by any request, so keeping it only made a group's
        failover list read one hop longer than it is -- and the Router spent that
        hop on a group with no deployments.
        """

        self._with_router_settings(
            """
            router_settings:
              fallbacks:
                - kimi-k3:
                    - retired-model
                    - gpt-5.6-sol
            """
        )

        from young_router.config import api as config_api, load as config_load

        document = config_api.config_document_from_path(self.path)
        result = config_api.save_config(
            config_load.load_config(self.path)["providers"], self.path, None, document,
        )
        saved = self._saved()
        self.assertEqual([{"kimi-k3": ["gpt-5.6-sol"]}], saved["router_settings"]["fallbacks"])
        self.assertEqual(["retired-model"], result["dropped_group_references"])

    def test_an_edit_that_keeps_every_name_never_rewrites_the_tables(self) -> None:
        """Only a rename moves the fallback graph; a limit edit leaves it alone.

        The deployment ids are what identify a rename.  An edit that keeps the
        names therefore restates nothing, which is what keeps a hand-written
        table out of the path of every unrelated save.
        """

        self._with_router_settings(
            """
            router_settings:
              fallbacks:
                - kimi-k3:
                    - gpt-5.6-sol
              model_group_alias:
                fast-sol: gpt-5.6-sol
            """
        )

        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"max_input_tokens": 372000}},
        )
        self.domain.apply()
        saved = self._saved()
        self.assertEqual([{"kimi-k3": ["gpt-5.6-sol"]}], saved["router_settings"]["fallbacks"])
        self.assertEqual(
            {"fast-sol": "gpt-5.6-sol"}, saved["router_settings"]["model_group_alias"]
        )

    def test_a_document_without_router_settings_gains_none(self) -> None:
        """The save never invents a section the file did not have."""

        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"name": "sol-next"}},
        )
        self.domain.apply()
        self.assertNotIn("router_settings", self._saved())

    def test_a_hop_names_the_group_in_the_case_the_document_states(self) -> None:
        """A fallback reference is the group's name, case included.

        The spelling is settled when a model is imported, and ``router_settings``
        names the group the document serves.  A reference that states another
        case names a group no route answers, so the Router would take the hop,
        find no deployment, and answer a 500 over the real upstream failure --
        the save drops that target and keeps the one whose case matches.  Here
        both spellings exist as groups, and each reference keeps the one it
        states.
        """

        self._with_router_settings(
            """
            router_settings:
              fallbacks:
                - kimi-k3:
                    - MiniMax-M3.1-Flash
                    - MINIMAX-M3.1-FLASH
              model_group_alias:
                fast-minimax: minimax-m3.1-flash
            """
        )

        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "kimi-k3", "changes": {"max_input_tokens": 262144}},
        )
        self.domain.apply()
        saved = self._saved()

        # The exact spelling is the group that answers; the other case is dropped.
        self.assertEqual(
            [{"kimi-k3": ["MiniMax-M3.1-Flash"]}],
            saved["router_settings"]["fallbacks"],
        )
        self.assertEqual(
            {"fast-minimax": "minimax-m3.1-flash"},
            saved["router_settings"]["model_group_alias"],
        )

    def test_the_catch_all_fallback_chain_survives_a_rename(self) -> None:
        """``"*"`` is the Router's catch-all key, not a group name.

        It is read for every request whose own group declares no fallbacks, so
        measuring it against the served group names would delete the one chain
        that answers for all of them.  Its *targets* are group names, so they move
        with a rename like every other fallback list.
        """

        self._with_router_settings(
            """
            router_settings:
              fallbacks:
                - "*":
                    - gpt-5.6-sol
                    - kimi-k3
            """
        )

        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"name": "sol-next"}},
        )
        self.domain.apply()
        saved = self._saved()
        self.assertEqual(
            [{"*": ["sol-next", "kimi-k3"]}], saved["router_settings"]["fallbacks"]
        )


class UnknownModelTakesItsRoutesStatedWindowTests(unittest.TestCase):
    """A model no profile knows is published with the window its route states.

    Observed live 2026-11-05: ``hy4free``/``hy4std`` on a WorkBuddy account
    state 960,000 in that account's own catalog, and the registry resolves the
    name from nothing -- no bundled profile, no pi.dev record, no LiteLLM entry.
    The unknown-model placeholder 272,000 was then published for both, on every
    surface at once: the pane's 公开上下文, the 推荐上下文 beside it, and the
    managed catalog Codex compacts against.  A client compacted a 960,000-token
    model at 272,000.

    The rule that produced that was written for the opposite case -- a lane
    whose capability table does not carry a model publishes its generic
    fallback, and that number is not a window.  This test states both halves:
    the reading answers where nothing else does, and never where a profile
    does.
    """

    def _domain(self, root: Path, names: str) -> ProvidersModelsDomain:
        config = root / "config.yaml"
        config.write_text(
            "providers:\n"
            "  primary:\n"
            '    api_base: "https://example.test/v1"\n'
            "    api_keys:\n"
            "      - name: default\n"
            '        value: "replace-me-secret"\n'
            "model_list:\n"
            f"  - model_name: {names}\n"
            "    litellm_params:\n"
            "      model: openai/fresh-model\n"
            f"      api_base: \"https://example.test/v1\"\n"
            "      api_key: \"replace-me-secret\"\n"
            "    model_info:\n"
            "      id: \"00000001\"\n"
            "      provider: primary\n",
            encoding="utf-8",
        )
        return ProvidersModelsDomain(config)

    def _write_pulse_profile(self, root: Path) -> None:
        """The pi.dev profile the 2026-11-04 incident was measured against."""

        (root / "model-contexts.json").write_text(
            json.dumps(
                {
                    "fetched_at": 1_000_000.0,
                    "records": {
                        "opencode/pulse-alpha-free": {
                            "context_window": 1_048_576,
                            "max_context_window": 1_048_576,
                            "effective_context_window_percent": 95,
                            "source": "https://pi.dev/api/models",
                            "priority": 40,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_a_name_no_profile_resolves_takes_the_window_its_upstream_stated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = self._domain(root, "fresh-model")
            domain._context_registry().remember_stated_windows({"fresh-model": 960_000})
            contexts = domain.snapshot()["model_contexts"]["fresh-model"]
            self.assertEqual(960_000, contexts["context_window"])
            self.assertEqual(960_000, contexts["max_context_window"])
            # The recommendation is the same number, because there is no pi.dev
            # profile to name beside it: reporting the placeholder there would
            # put two figures on one model and invite 设置 to write the wrong one.
            self.assertEqual(960_000, contexts["recommended_context_window"])
            # The managed catalog is written through the same resolution, so the
            # two surfaces cannot disagree about the number a client compacts
            # against.
            catalog = catalog_payload(
                ["fresh-model"], registry=domain._context_registry()
            )
            self.assertEqual(960_000, catalog["models"][0]["context_window"])

    def test_a_profile_still_wins_over_the_number_an_upstream_advertises(self) -> None:
        """The 2026-11-04 incident: a lane's fallback is not a window.

        ``pulse-alpha-free`` resolves through the registry's own record, so a
        catalog read stating a smaller number -- that lane advertised the model's
        output ceiling, 131,072, against a 1,048,576 context -- must not move it.
        This is the half of the rule that keeps the fix above from undoing the
        protection it sits beside.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_pulse_profile(root)
            config = root / "config.yaml"
            config.write_text(
                "providers:\n"
                "  primary:\n"
                '    api_base: "https://example.test/v1"\n'
                "    api_keys:\n"
                "      - name: default\n"
                '        value: "replace-me-secret"\n'
                "model_list:\n"
                "  - model_name: pulse-alpha-free\n"
                "    litellm_params:\n"
                "      model: openai/pulse-alpha-free\n"
                '      api_base: "https://example.test/v1"\n'
                '      api_key: "replace-me-secret"\n'
                "    model_info:\n"
                '      id: "00000001"\n'
                "      provider: primary\n",
                encoding="utf-8",
            )
            domain = ProvidersModelsDomain(config)
            domain._context_registry().remember_stated_windows(
                {"pulse-alpha-free": 131_072}
            )
            contexts = domain.snapshot()["model_contexts"]["pulse-alpha-free"]
            self.assertEqual(1_048_576, contexts["context_window"])
            # The reading is still kept, and it is still only a reading: the
            # cache records it for the row that says whose number it is, while
            # the resolved window above stayed the profile's.
            self.assertEqual(
                131_072,
                domain._context_registry().stated_windows().get("pulse-alpha-free"),
            )

    def test_a_declared_window_is_never_replaced_by_a_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = self._domain(root, "fresh-model")
            domain.dispatch(
                "public.model_patch",
                {"public_model": "fresh-model", "changes": {"max_input_tokens": 64_000}},
            )
            domain.apply()
            domain._context_registry().remember_stated_windows({"fresh-model": 960_000})
            contexts = domain.snapshot()["model_contexts"]["fresh-model"]
            self.assertEqual(64_000, contexts["context_window"])
            # The declaration is the group's own, and it stays untouched by the
            # route's reading on disk as well.
            self.assertEqual(
                64_000,
                next(
                    model["max_input_tokens"]
                    for provider in domain.snapshot()["providers"]
                    for model in provider["models"]
                ),
            )

class ContradictedInferenceTakesTheUpstreamsWindowTests(unittest.TestCase):
    """A suffix match that picked an outlier loses to the serving upstream.

    Observed live 2026-11-05: ``minimax-m3.1-flash`` on a WorkBuddy account was
    published at 262,144 while that account's own catalog states 1,000,000.  The
    public name is in no record itself, so it resolved by suffix match over every
    provider's copy of the id: seven state 1,000,000 or 1,048,576 and one --
    nvidia's -- states 262,144, and ``_lookup`` takes the smallest of the
    top-priority set.  The group was published at a quarter of what its own
    upstream will accept.

    The rule that decides it reads both sides.  The upstream must agree with the
    copies the inference passed over; when it agrees with nobody its number is
    the 2026-11-04 fallback, a reading for its own row and not a window -- which
    is the case this test's sibling pins.
    """

    PROFILE = {
        "context_window": 1_000_000,
        "max_context_window": 1_000_000,
        "effective_context_window_percent": 95,
        "source": "https://pi.dev/api/models",
        "priority": 40,
    }

    def _registry(self, root: Path, copies: Mapping[str, int]) -> ModelContextRegistry:
        (root / "model-contexts.json").write_text(
            json.dumps(
                {
                    "fetched_at": 1_000_000.0,
                    "records": {
                        key: {**self.PROFILE, "context_window": window, "max_context_window": window}
                        for key, window in copies.items()
                    },
                }
            ),
            encoding="utf-8",
        )
        return ModelContextRegistry(
            runtime_config_path=root / "config.yaml",
            cache_path=root / "model-contexts.json",
            refresh_enabled=False,
        )

    def _domain(self, root: Path, name: str, wire: str) -> ProvidersModelsDomain:
        config = root / "config.yaml"
        config.write_text(
            "providers:\n"
            "  primary:\n"
            '    api_base: "https://example.test/v1"\n'
            "    api_keys:\n"
            "      - name: default\n"
            '        value: "replace-me-secret"\n'
            "model_list:\n"
            f"  - model_name: {name}\n"
            "    litellm_params:\n"
            f"      model: openai/{wire}\n"
            '      api_base: "https://example.test/v1"\n'
            '      api_key: "replace-me-secret"\n'
            "    model_info:\n"
            '      id: "00000001"\n'
            "      provider: primary\n",
            encoding="utf-8",
        )
        return ProvidersModelsDomain(config)

    def test_the_group_takes_the_window_its_own_upstream_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._registry(
                root,
                {
                    "minimax/minimax-m3.1-flash": 1_000_000,
                    "minimax-cn/minimax-m3.1-flash": 1_000_000,
                    "opencode-go/minimax-m3.1-flash": 1_000_000,
                    "huggingface/minimaxai/minimax-m3.1-flash": 1_000_000,
                    "openrouter/minimax/minimax-m3.1-flash": 1_048_576,
                    "nvidia/minimaxai/minimax-m3.1-flash": 262_144,
                },
            )
            domain = self._domain(root, "minimax-m3.1-flash", "minimax-m3.1-flash")
            registry = domain._context_registry()
            self.assertEqual(262_144, registry.record_for("minimax-m3.1-flash").context_window)
            registry.remember_stated_windows({"minimax-m3.1-flash": 1_000_000})
            contexts = domain.snapshot()["model_contexts"]["minimax-m3.1-flash"]
            self.assertEqual(1_000_000, contexts["context_window"])
            self.assertEqual(1_000_000, contexts["recommended_context_window"])

    def test_an_upstream_that_agrees_with_no_copy_keeps_its_reading(self) -> None:
        """The 2026-11-04 shape: the lane's fallback is not a window.

        One copy, agreeing with the upstream, is not a contradiction -- the
        upstream disagrees with the registry here, and the rule requires the
        upstream to agree with a copy the inference passed over.  So the lane's
        131,072 stays the reading in its own row while the published window is
        the model's 1,048,576.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._registry(root, {"opencode/pulse-alpha-free": 1_048_576})
            domain = self._domain(root, "pulse-alpha-free", "pulse-alpha-free")
            registry = domain._context_registry()
            registry.remember_stated_windows({"pulse-alpha-free": 131_072})
            contexts = domain.snapshot()["model_contexts"]["pulse-alpha-free"]
            self.assertEqual(1_048_576, contexts["context_window"])

    def test_an_exact_profile_is_never_contradicted(self) -> None:
        """A record that names the id is a lookup, not an inference.

        ``deepseek-v4.1-flash`` is stated exactly by several providers, so no
        catalog read may move it however different the number it states.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._registry(root, {"deepseek/deepseek-v4.1-flash": 1_000_000})
            domain = self._domain(root, "deepseek-v4.1-flash", "deepseek-v4.1-flash")
            registry = domain._context_registry()
            registry.remember_stated_windows({"deepseek-v4.1-flash": 64_000})
            contexts = domain.snapshot()["model_contexts"]["deepseek-v4.1-flash"]
            self.assertEqual(1_000_000, contexts["context_window"])


if __name__ == "__main__":
    unittest.main()
