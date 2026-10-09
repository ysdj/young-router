from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from young_router.core.domains import DomainError
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.domains.relay_accounts import RelayAccountsDomain
from young_router.core.persistence import atomic_write_json
from young_router.core.provider_auth import ProviderAuthManager
from young_router.core.service import CoreStore


class ServiceProviderBoundaryTests(unittest.TestCase):
    def _domain(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        return directory, ProvidersModelsDomain(
            config, auth_manager=ProviderAuthManager(root)
        )

    def test_a_providers_type_is_never_retargeted_in_place(self) -> None:
        """A type is what a provider *is*, so no patch changes it.

        A provider's type owns its account contract, its address, its key slot,
        and every route's protocol surface.  The pane therefore states it
        read-only, and Core refuses a patch that asks for a different one
        instead of rewiring a live provider half-way — a refused patch must
        leave the document exactly as it was.  The two refusals are separate
        facts: the editor may not touch an account-backed provider at all, and
        no surface may retarget a key provider.
        """

        directory, domain = self._domain()
        with directory:
            with self.assertRaisesRegex(DomainError, "Service Provider Management"):
                domain.dispatch(
                    "provider.add",
                    {"provider": {"name": "OpenAI", "auth_kind": "openai_login"}},
                )

            snapshot = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "Custom",
                        "api_base": "https://api.example.test/v1",
                        "create_default_api_key": True,
                        "models": [
                            {"name": "m1", "upstream_model": "m1", "enabled": True, "order": 1}
                        ],
                    }
                },
            )
            provider_id = snapshot["providers"][0]["id"]
            for requested in ("claude_login", "workbuddy_login", "workbuddy_ai_login"):
                with self.assertRaisesRegex(DomainError, "type is fixed"):
                    domain.dispatch(
                        "provider.patch",
                        {"provider_id": provider_id, "changes": {"auth_kind": requested}},
                    )
            # The refusal is not partial: the address, the key slot, and every
            # route stay exactly as the create wrote them.
            provider = domain.snapshot()["providers"][0]
            self.assertEqual(provider["auth_kind"], "api_key")
            self.assertEqual(provider["api_base"], "https://api.example.test/v1")
            self.assertEqual(provider["api_key_names"], ["default"])
            self.assertIs(provider["enabled"], True)
            self.assertEqual(provider["models"][0]["upstream_protocol_mode"], "fallback")
            # A type outside the contract is refused as an unavailable type, so
            # a misspelling is never read as a real one.
            with self.assertRaisesRegex(DomainError, "login type is unavailable"):
                domain.dispatch(
                    "provider.patch",
                    {"provider_id": provider_id, "changes": {"auth_kind": "bogus_login"}},
                )
            # Naming the type the provider already holds is a no-op, not a
            # retarget: the same patch from any client stays idempotent.
            unchanged = domain.dispatch(
                "provider.patch",
                {"provider_id": provider_id, "changes": {"auth_kind": "api_key"}},
            )["providers"][0]
            self.assertEqual(unchanged["auth_kind"], "api_key")
            self.assertEqual(unchanged["api_base"], "https://api.example.test/v1")
            self.assertEqual(unchanged["api_key_names"], ["default"])

    def test_the_service_surface_states_a_type_and_never_switches_it(self) -> None:
        """The account side is the same fact from the other end.

        ``service_provider.patch`` owns a login provider's name and enabled
        state; its type is stated there too, and a patch that asks for another
        one is refused rather than performed.  That is what makes the pane's
        read-only type row honest: there is no action behind it at all.
        """

        directory, domain = self._domain()
        with directory:
            added = domain.dispatch("service_provider.add", {"kind": "claude_login"})
            provider_id = added["providers"][0]["id"]
            for requested in ("openai_login", "workbuddy_login", "api_key"):
                with self.assertRaisesRegex(DomainError, "type is fixed"):
                    domain.dispatch(
                        "service_provider.patch",
                        {"provider_id": provider_id, "provider": {"auth_kind": requested}},
                    )
            provider = domain.snapshot()["providers"][0]
            self.assertEqual(provider["auth_kind"], "claude_login")
            self.assertEqual(provider["api_key_names"], ["claude-oauth"])
            # The name and the enabled state are still the service surface's
            # own edits, and stating the type it already holds changes nothing.
            renamed = domain.dispatch(
                "service_provider.patch",
                {"provider_id": provider_id, "provider": {"name": "Claude 2", "auth_kind": "claude_login"}},
            )["providers"][0]
            self.assertEqual(renamed["name"], "Claude 2")
            self.assertEqual(renamed["auth_kind"], "claude_login")

    def test_api_key_provider_names_are_unique_case_insensitively(self) -> None:
        directory, domain = self._domain()
        with directory:
            domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "Atlas",
                        "api_base": "https://atlas.example/v1",
                        "models": [],
                    }
                },
            )
            with self.assertRaisesRegex(DomainError, "already exists"):
                domain.dispatch(
                    "provider.add",
                    {
                        "provider": {
                            "name": " atlas ",
                            "api_base": "https://other.example.test/v1",
                            "models": [],
                        }
                    },
                )
            self.assertEqual(1, len(domain.snapshot()["providers"]))

            second = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "Other",
                        "api_base": "https://other.example.test/v1",
                        "models": [],
                    }
                },
            )
            before_names = [item["name"] for item in domain.snapshot()["providers"]]
            with self.assertRaisesRegex(DomainError, "already exists"):
                domain.dispatch(
                    "provider.patch",
                    {
                        "provider_id": second["providers"][1]["id"],
                        "changes": {"name": "ATLAS"},
                    },
                )
            self.assertEqual(before_names, [item["name"] for item in domain.snapshot()["providers"]])

    def test_relay_station_name_collision_is_case_insensitive(self) -> None:
        directory, domain = self._domain()
        with directory:
            domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "Atlas",
                        "api_base": "https://atlas.example/v1",
                        "models": [],
                    }
                },
            )
            second = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "Other",
                        "api_base": "https://other.example.test/v1",
                        "models": [],
                    }
                },
            )
            with self.assertRaisesRegex(DomainError, "already exists"):
                domain.dispatch(
                    "provider.select_relay_station",
                    {
                        "provider_id": second["providers"][1]["id"],
                        "source": {
                            "station_id": "station-atlas",
                            "name": "ATLAS",
                            "api_base": "https://relay.example.test/v1",
                        },
                    },
                )
            self.assertEqual("Atlas", domain.snapshot()["providers"][0]["name"])
            self.assertEqual("Other", domain.snapshot()["providers"][1]["name"])

    def _relay_core(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        providers = ProvidersModelsDomain(config, auth_manager=ProviderAuthManager(root))
        relay = RelayAccountsDomain(root)
        core = CoreStore(domains=[relay, providers])
        relay.dispatch(
            "account.add",
            {"type": "sub2api", "label": "account", "origin": "https://atlas.example", "station_name": "atlas"},
        )
        station_id = relay.snapshot()["stations"][0]["id"]
        return directory, providers, relay, core, station_id

    def test_station_binding_preserves_a_matching_api_base_and_stays_clean(self) -> None:
        """Binding a same-name, same-site provider must not dirty the draft.

        The provider keeps its ``/v1`` spelling and the hidden source
        metadata change stays clean, so closing the window never asks the
        user to discard a change that altered nothing visible.
        """

        directory, providers, _relay, core, station_id = self._relay_core()
        with directory:
            added = providers.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "atlas",
                        "api_base": "https://atlas.example/v1",
                        "models": [],
                        "create_default_api_key": True,
                    }
                },
            )
            provider_id = added["providers"][0]["id"]
            result = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.select_relay_station",
                    "payload": {"provider_id": provider_id, "station_id": station_id},
                },
                expected_revision=core.revision,
            )
            provider = providers.draft_state()["providers"][0]
            self.assertEqual("https://atlas.example/v1", provider["api_base"])
            self.assertEqual("relay", provider["provider_type"])
            self.assertEqual(station_id, provider["relay_station_id"])
            self.assertFalse(core.snapshot()["drafts"]["providers_models"]["dirty"])
            self.assertEqual(core.revision, result["revision"])

    def test_reselecting_the_same_station_is_a_no_op(self) -> None:
        directory, providers, _relay, core, station_id = self._relay_core()
        with directory:
            added = providers.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "atlas",
                        "api_base": "https://atlas.example/v1",
                        "models": [],
                        "create_default_api_key": True,
                    }
                },
            )
            provider_id = added["providers"][0]["id"]
            first = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.select_relay_station",
                    "payload": {"provider_id": provider_id, "station_id": station_id},
                },
                expected_revision=core.revision,
            )
            before = providers.draft_state()["providers"][0]
            second = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.select_relay_station",
                    "payload": {"provider_id": provider_id, "station_id": station_id},
                },
                expected_revision=first["revision"],
            )
            self.assertEqual(before, providers.draft_state()["providers"][0])
            self.assertFalse(core.snapshot()["drafts"]["providers_models"]["dirty"])

    def test_station_binding_keeps_a_different_site_substantive(self) -> None:
        """A host-level site mismatch (www vs bare) rewrites the URL and stays dirty."""

        directory, providers, _relay, core, station_id = self._relay_core()
        with directory:
            added = providers.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "atlas-www",
                        "api_base": "https://www.atlas.example/v1",
                        "models": [],
                        "create_default_api_key": True,
                    }
                },
            )
            provider_id = added["providers"][0]["id"]
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.select_relay_station",
                    "payload": {"provider_id": provider_id, "station_id": station_id},
                },
                expected_revision=core.revision,
            )
            provider = providers.draft_state()["providers"][0]
            self.assertEqual("https://atlas.example", provider["api_base"])
            self.assertEqual("atlas", provider["name"])
            self.assertTrue(core.snapshot()["drafts"]["providers_models"]["dirty"])

    def test_service_provider_defaults_name_and_model(self) -> None:
        directory, domain = self._domain()
        with directory:
            snapshot = domain.dispatch("service_provider.add", {"kind": "openai_login"})
            provider = snapshot["providers"][0]
            self.assertEqual("OpenAI", provider["name"])
            self.assertEqual("openai_login", provider["auth_kind"])
            self.assertEqual("chatgpt/gpt-5.4", provider["models"][0]["litellm_model"])

            with self.assertRaisesRegex(DomainError, "already exists"):
                domain.dispatch("service_provider.add", {"kind": "openai_login"})

            claude = domain.dispatch(
                "service_provider.add", {"kind": "claude_login"}
            )["providers"][1]
            self.assertEqual("Claude", claude["name"])
            self.assertEqual(
                "anthropic/claude-sonnet-4-5", claude["models"][0]["litellm_model"]
            )

    def test_multiple_same_kind_accounts_have_distinct_refs(self) -> None:
        directory, domain = self._domain()
        with directory:
            first = domain.dispatch(
                "service_provider.add", {"kind": "openai_login", "name": "OpenAI One"}
            )
            second = domain.dispatch(
                "service_provider.add", {"kind": "openai_login", "name": "OpenAI Two"}
            )
            providers = domain.draft_state()["providers"]
            self.assertEqual(2, len(providers))
            first_ref = domain._provider_auth_state(providers[0])["credential_ref"]
            second_ref = domain._provider_auth_state(providers[1])["credential_ref"]
            self.assertNotEqual(first_ref, second_ref)
            self.assertTrue(providers[0]["enabled"])
            self.assertFalse(providers[1]["enabled"])
            self.assertFalse(first["providers"][0]["auth_active"])
            self.assertFalse(second["providers"][1]["auth_active"])

    def test_auth_actions_use_selected_provider_ref(self) -> None:
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        manager = Mock()
        manager.status.return_value = {"status": "signed_out", "configured": False}
        domain = ProvidersModelsDomain(config, auth_manager=manager)
        try:
            domain.dispatch("service_provider.add", {"kind": "claude_login", "name": "Claude One"})
            domain.dispatch("service_provider.add", {"kind": "claude_login", "name": "Claude Two"})
            providers = domain.draft_state()["providers"]
            first_id = domain.snapshot()["providers"][0]["id"]
            second_id = domain.snapshot()["providers"][1]["id"]
            domain.dispatch("service_provider.auth_status", {"provider_id": first_id})
            domain.dispatch("service_provider.auth_status", {"provider_id": second_id})
            refs = [call.args[1] for call in manager.status.call_args_list]
            first_ref = domain._provider_auth_state(providers[0])["credential_ref"]
            second_ref = domain._provider_auth_state(providers[1])["credential_ref"]
            self.assertIn(first_ref, refs)
            self.assertIn(second_ref, refs)
            self.assertNotEqual(first_ref, second_ref)
        finally:
            directory.cleanup()

    def test_auth_status_poll_does_not_advance_core_revision_when_draft_is_unchanged(self) -> None:
        directory, domain = self._domain()
        with directory:
            domain.dispatch("service_provider.add", {"kind": "claude_login"})
            provider = domain.snapshot()["providers"][0]
            provider_id = provider["id"]
            core = CoreStore(domains=[domain])
            before = core.revision
            domain_before = domain.revision
            first = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "service_provider.auth_status",
                    "payload": {"provider_id": provider_id},
                },
                expected_revision=before,
            )
            second = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "service_provider.auth_status",
                    "payload": {"provider_id": provider_id},
                },
                expected_revision=first["revision"],
            )
            self.assertEqual(before, first["revision"])
            self.assertEqual(before, second["revision"])
            self.assertEqual(domain_before, domain.revision)

    def test_auth_status_poll_advances_revision_when_it_changes_runtime_routing(self) -> None:
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        manager = Mock()
        manager.status.return_value = {"status": "signed_out", "configured": False}
        domain = ProvidersModelsDomain(config, auth_manager=manager)
        with directory:
            domain.dispatch("service_provider.add", {"kind": "openai_login"})
            provider_id = domain.snapshot()["providers"][0]["id"]
            self.assertTrue(domain.draft_state()["providers"][0]["enabled"])
            core = CoreStore(domains=[domain])
            before = core.revision
            result = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "service_provider.auth_status",
                    "payload": {"provider_id": provider_id},
                },
                expected_revision=before,
            )
            self.assertGreater(result["revision"], before)
            self.assertFalse(domain.draft_state()["providers"][0]["enabled"])

    def test_a_workbuddy_read_answers_with_its_own_action_summary(self) -> None:
        """The catalog read a rate row asks for travels back with that read.

        A live read stages nothing but still advances Core's shared revision,
        and the provider pane's own account read runs alongside the model
        pane's catalog read.  The catalog must therefore come back on the
        dispatch that asked for it: reading the shared slot afterwards returns
        whichever read landed last, and a rate row that receives an account
        document builds no rate at all — the 倍率 row then reads 无 for the
        whole cache interval.
        """

        directory, domain = self._domain()
        with directory:
            models = [
                {
                    "id": "glm-5.3",
                    "name": "GLM-5.3",
                    "billing": {"credits": "x0.79 credits", "free": False},
                }
            ]
            catalog_summary = {
                "operation": "workbuddy_models",
                "provider": "workbuddy",
                "display_name": "WorkBuddy",
                "available": True,
                "models": models,
            }
            status_summary = {
                "operation": "workbuddy_status",
                "available": True,
                "detail": "",
                "providers": {"workbuddy": {"state": "signed-in"}},
            }

            def dispatch_operation(action: str, data):  # type: ignore[no-untyped-def]
                domain._last_operation = (
                    catalog_summary if action == "workbuddy_models" else status_summary
                )
                return {"operation_summary": domain._last_operation}

            core = CoreStore(domains=[domain])
            with patch.object(domain, "dispatch", side_effect=dispatch_operation):
                fetched = core.dispatch(
                    {
                        "domain": "providers_models",
                        "type": "workbuddy_models",
                        "payload": {"provider": "workbuddy"},
                    },
                    expected_revision=core.revision,
                )
            self.assertEqual(catalog_summary, fetched.get("action_summary"))

    def test_a_workbuddy_account_read_answers_with_its_own_summary(self) -> None:
        """Every WorkBuddy read names itself, not just the catalog one."""

        directory, domain = self._domain()
        with directory:
            core = CoreStore(domains=[domain])
            result = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "workbuddy_status",
                    "payload": {"refresh": True},
                },
                expected_revision=core.revision,
            )
            summary = result.get("action_summary")
            self.assertIsInstance(summary, dict)
            self.assertEqual("workbuddy_status", summary["operation"])
            self.assertEqual(
                domain._last_operation,
                core.snapshot()["action_summaries"]["providers_models"]["operation_summary"],
            )

    def test_dispatch_returns_only_the_current_model_fetch_summary(self) -> None:
        directory, domain = self._domain()
        with directory:
            added = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "OpenRouter",
                        "api_base": "https://openrouter.ai/api/v1",
                        "create_default_api_key": True,
                    }
                },
            )
            provider_id = added["providers"][0]["id"]
            summary = {
                "operation": "fetch_models",
                "provider_id": provider_id,
                "protocols": ["openai-models-v1"],
                "api_key_name": "default",
                "available": True,
                "detail": "Provider model list fetched",
                "models": ["model-a"],
                "model_count": 1,
            }
            core = CoreStore(domains=[domain])
            with patch.object(domain, "_fetch_provider_models", return_value=summary):
                fetched = core.dispatch(
                    {
                        "domain": "providers_models",
                        "type": "providers.fetch_models",
                        "payload": {"provider_id": provider_id},
                    },
                    expected_revision=core.revision,
                )
            self.assertEqual(summary, fetched["action_summary"])

            edited = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.add",
                    "payload": {
                        "provider_id": provider_id,
                        "model": {"name": "model-b", "upstream_model": "model-b"},
                    },
                },
                expected_revision=core.revision,
            )
            self.assertNotIn("action_summary", edited)

    def test_openai_activation_switches_active_slot_and_delete_isolated(self) -> None:
        directory, domain = self._domain()
        with directory:
            domain.dispatch(
                "service_provider.add",
                {"kind": "openai_login", "name": "OpenAI One"},
            )
            domain.dispatch(
                "service_provider.add",
                {"kind": "openai_login", "name": "OpenAI Two"},
            )
            providers = domain.draft_state()["providers"]
            first_ref = domain._provider_auth_state(providers[0])["credential_ref"]
            second_ref = domain._provider_auth_state(providers[1])["credential_ref"]
            manager = domain._auth_manager()
            for ref in (first_ref, second_ref):
                auth_file = manager._secure_chatgpt_auth_file(ref, create=True)
                atomic_write_json(
                    auth_file,
                    {"access_token": f"token-{ref}", "expires_at": 4102444800},
                )
            ids = [provider["id"] for provider in domain.snapshot()["providers"]]

            result = domain.dispatch("service_provider.auth_activate", {"provider_id": ids[0]})
            self.assertTrue(result["operation_summary"]["requires_restart"])
            self.assertEqual(first_ref, manager.active_openai_ref())
            active = domain.snapshot()["providers"]
            self.assertTrue(active[0]["auth_active"])
            self.assertFalse(active[1]["auth_active"])
            self.assertTrue(domain.draft_state()["providers"][0]["enabled"])
            self.assertFalse(domain.draft_state()["providers"][1]["enabled"])

            domain.dispatch("service_provider.activate", {"provider_id": ids[1]})
            self.assertEqual(second_ref, manager.active_openai_ref())
            active = domain.snapshot()["providers"]
            self.assertFalse(active[0]["auth_active"])
            self.assertTrue(active[1]["auth_active"])
            self.assertFalse(domain.draft_state()["providers"][0]["enabled"])
            self.assertTrue(domain.draft_state()["providers"][1]["enabled"])

            domain.dispatch("service_provider.delete", {"provider_id": ids[1]})
            self.assertEqual("", manager.active_openai_ref())
            self.assertEqual("signed_in", manager.status("openai_login", first_ref)["status"])
            self.assertEqual(1, len(domain.snapshot()["providers"]))

    def test_openai_activation_rejects_unsigned_account(self) -> None:
        directory, domain = self._domain()
        with directory:
            domain.dispatch(
                "service_provider.add",
                {"kind": "openai_login", "name": "OpenAI"},
            )
            provider_id = domain.snapshot()["providers"][0]["id"]
            with self.assertRaisesRegex(DomainError, "could not be activated"):
                domain.dispatch("service_provider.activate", {"provider_id": provider_id})

    def test_openai_logout_disables_selected_account_only(self) -> None:
        directory, domain = self._domain()
        with directory:
            domain.dispatch("service_provider.add", {"kind": "openai_login", "name": "One"})
            domain.dispatch("service_provider.add", {"kind": "openai_login", "name": "Two"})
            providers = domain.draft_state()["providers"]
            first_ref = domain._provider_auth_state(providers[0])["credential_ref"]
            second_ref = domain._provider_auth_state(providers[1])["credential_ref"]
            manager = domain._auth_manager()
            for ref in (first_ref, second_ref):
                atomic_write_json(
                    manager._secure_chatgpt_auth_file(ref, create=True),
                    {"access_token": f"token-{ref}", "expires_at": 4102444800},
                )
            first_id = domain.snapshot()["providers"][0]["id"]
            domain.dispatch("service_provider.activate", {"provider_id": first_id})
            domain.dispatch("service_provider.auth_logout", {"provider_id": first_id})
            self.assertEqual("", manager.active_openai_ref())
            self.assertFalse(domain.draft_state()["providers"][0]["enabled"])
            self.assertEqual("signed_in", manager.status("openai_login", second_ref)["status"])

            # A later login auto-selects the first account again; its status
            # poll restores the only enabled runtime route in the draft.
            atomic_write_json(
                manager._secure_chatgpt_auth_file(first_ref, create=True),
                {"access_token": f"token-{first_ref}", "expires_at": 4102444800},
            )
            manager.activate("openai_login", first_ref)
            domain.dispatch("service_provider.auth_status", {"provider_id": first_id})
            self.assertTrue(domain.draft_state()["providers"][0]["enabled"])
            self.assertFalse(domain.draft_state()["providers"][1]["enabled"])

    def test_a_new_model_never_claims_a_provider_key_by_itself(self) -> None:
        directory, domain = self._domain()
        with directory:
            config = Path(directory.name) / "config.yaml"
            added = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "novai",
                        "api_base": "https://novai.test/v1",
                        "create_default_api_key": True,
                        "initial_api_key_name": "x",
                        "models": [],
                    }
                },
            )
            provider_id = added["providers"][0]["id"]
            domain.dispatch(
                "provider.patch",
                {
                    "provider_id": provider_id,
                    "changes": {"api_keys": [{"name": "x", "value": "test-secret-placeholder"}]},
                },
            )
            domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {"name": "novai-model", "upstream_model": "novai-model", "enabled": True, "order": 0},
                },
            )

            model = domain.snapshot()["providers"][0]["models"][0]
            # The editor must not pick a key for the user: a model that was
            # never bound stays unbound.
            self.assertEqual("", model["provider_key_id"])
            self.assertEqual("", model["api_key_name"])

            domain.apply()
            # Materialization still routes the model through the provider's
            # default key, so an unbound draft keeps working at runtime.
            document = config.read_text(encoding="utf-8")
            self.assertIn("api_key: *novai_api_key_x", document)
            self.assertIn("api_key_name: x", document)

            # Binding a key explicitly still sticks, and going back to the
            # default drops the model's own binding again.
            bound = domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": model["editor_id"],
                    "changes": {"api_key_name": "x"},
                },
            )["providers"][0]["models"][0]
            self.assertEqual("x", bound["api_key_name"])

            unbound = domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": model["editor_id"],
                    "changes": {"provider_key_id": "", "api_key_name": ""},
                },
            )["providers"][0]["models"][0]
            self.assertEqual("", unbound["provider_key_id"])
            self.assertEqual("", unbound["api_key_name"])

    def test_validation_names_an_enabled_model_without_a_public_name(self) -> None:
        directory, domain = self._domain()
        with directory:
            added = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "Example",
                        "api_base": "https://example.test/v1",
                        "create_default_api_key": True,
                        "models": [
                            {"model_name": "model-one", "litellm_model": "model-one"},
                            {"model_name": "", "litellm_model": "", "enabled": True, "order": 0},
                            {"model_name": "", "litellm_model": "", "enabled": False},
                        ],
                    }
                },
            )
            domain.dispatch(
                "provider.patch",
                {
                    "provider_id": added["providers"][0]["id"],
                    "changes": {"api_keys": [{"name": "default", "value": "test-secret-placeholder"}]},
                },
            )

            # The shared pane needs the provider and the row: the validation
            # result names both instead of reporting one opaque failure, and an
            # entry that never materializes a route stays out of the result.
            validation = domain.validate()
            self.assertFalse(validation["valid"])
            self.assertEqual(
                ["model_name_required"],
                [issue["code"] for issue in validation["issues"]],
            )
            # The path survives the shared issue sanitizer, which drops any
            # location that is not a plain identifier.
            self.assertEqual("providers_models.Example.models[2]", validation["issues"][0]["path"])

            blank_model_id = domain.snapshot()["providers"][0]["models"][1]["editor_id"]
            domain.dispatch(
                "model.patch",
                {
                    "provider_id": added["providers"][0]["id"],
                    "model_id": blank_model_id,
                    "changes": {"name": "model-two"},
                },
            )
            self.assertTrue(domain.validate()["valid"])

    def test_validation_issue_paths_survive_the_shared_sanitizer(self) -> None:
        # A rejected Apply shows the location from this path; the shared
        # sanitizer keeps it only while it reads as a plain identifier.
        from young_router.core.service import _safe_issue_path

        self.assertEqual(
            "providers_models.Example.models[2]",
            _safe_issue_path("providers_models.Example.models[2]"),
        )
        self.assertEqual("configuration", _safe_issue_path("/Users/example/config.yaml"))
        self.assertEqual("configuration", _safe_issue_path("Example / #2"))

    def test_a_stale_slot_id_heals_through_the_key_name(self) -> None:
        directory, domain = self._domain()
        with directory:
            added = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "novai",
                        "api_base": "https://novai.test/v1",
                        "create_default_api_key": True,
                        "initial_api_key_name": "x",
                        "models": [],
                    }
                },
            )
            provider_id = added["providers"][0]["id"]
            domain.dispatch(
                "provider.patch",
                {
                    "provider_id": provider_id,
                    "changes": {"api_keys": [{"name": "x", "value": "test-secret-placeholder"}]},
                },
            )
            domain.dispatch(
                "model.add",
                {"provider_id": provider_id, "model": {"model_name": "m", "litellm_model": "m"}},
            )
            provider = domain._draft["providers"][0]
            key_id = domain._provider_api_keys(provider)[0]["id"]
            # A slot id can move while the model keeps the key name; the binding
            # must heal through the name instead of dropping to "no key".
            provider["models"][0]["provider_key_id"] = "provider-slot-00000000000000000000000000000000"
            provider["models"][0]["api_key_name"] = "x"

            domain._normalize_provider_model_bindings(domain._draft["providers"])

            model = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual(key_id, model["provider_key_id"])
            self.assertEqual("x", model["api_key_name"])

    def test_editor_ids_survive_a_failed_apply(self) -> None:
        # A rejected Apply rolls the draft back from a deep copy.  The editor
        # ids are the pane's handles: regenerating them stranded every
        # provider-scoped action (deleting a new model answered "The selected
        # provider is unavailable").
        import tempfile as _tempfile
        from pathlib import Path as _Path

        from young_router.core.service import CoreStore

        with _tempfile.TemporaryDirectory() as directory:
            config = _Path(directory) / "config.yaml"
            config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
            domain = ProvidersModelsDomain(config, auth_manager=ProviderAuthManager(_Path(directory)))
            core = CoreStore(domains=[domain])

            def provider() -> dict:
                return core.snapshot()["domains"]["providers_models"]["providers"][0]

            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.add",
                    "payload": {
                        "provider": {
                            "name": "novai",
                            "api_base": "https://novai.test/v1",
                            "create_default_api_key": True,
                            "initial_api_key_name": "x",
                            "models": [],
                        }
                    },
                },
                expected_revision=core.revision,
            )
            provider_id = provider()["id"]
            core.stage_secret(
                "providers_models",
                "api_key",
                f"{provider_id}\u001fx",
                "test-secret-placeholder",
                revision=core.revision,
            )
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.add",
                    "payload": {"provider_id": provider_id, "model": {"name": "", "upstream_model": "", "enabled": True, "order": 0}},
                },
                expected_revision=core.revision,
            )
            before_editor = provider()["editor_id"]
            model_id = provider()["models"][0]["id"]

            with self.assertRaises(Exception):
                core.apply(domains=["providers_models"], revision=core.revision)

            self.assertEqual(before_editor, provider()["editor_id"])
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.delete",
                    "payload": {"provider_id": before_editor, "model_id": model_id},
                },
                expected_revision=core.revision,
            )
            self.assertEqual([], provider()["models"])

    def test_validation_rejects_legacy_multiple_enabled_openai_accounts(self) -> None:
        directory, domain = self._domain()
        with directory:
            domain.dispatch("service_provider.add", {"kind": "openai_login", "name": "One"})
            domain.dispatch("service_provider.add", {"kind": "openai_login", "name": "Two"})
            for provider in domain._draft["providers"]:
                provider["enabled"] = True
            validation = domain.validate()
            self.assertFalse(validation["valid"])
            self.assertIn("Only one OpenAI login provider", validation["errors"][0])

    def test_existing_login_provider_is_readable_but_normal_patch_is_blocked(self) -> None:
        directory, domain = self._domain()
        with directory:
            domain._draft["providers"].append(
                {
                    "name": "Legacy OpenAI",
                    "api_base": "",
                    "auth_kind": "openai_login",
                    "auth_credential_ref": "chatgpt-account",
                    "extra": {
                        "x-young-router-provider-auth": {
                            "kind": "openai_login",
                            "credential_ref": "chatgpt-account",
                        }
                    },
                    "models": [],
                }
            )
            provider = domain.snapshot()["providers"][0]
            self.assertEqual("openai_login", provider["auth_kind"])
            with self.assertRaisesRegex(DomainError, "Service Provider Management"):
                domain.dispatch(
                    "provider.patch",
                    {
                        "provider_id": provider["id"],
                        "changes": {"name": "Renamed"},
                    },
                )
            domain.dispatch(
                "service_provider.patch",
                {
                    "provider_id": provider["id"],
                    "changes": {"name": "Renamed"},
                },
            )
            self.assertEqual("Renamed", domain.snapshot()["providers"][0]["name"])


if __name__ == "__main__":
    unittest.main()
