from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from litellm_menu.core.domains import DomainError
from litellm_menu.core.domains.providers_models import ProvidersModelsDomain
from litellm_menu.core.persistence import atomic_write_json
from litellm_menu.core.provider_auth import ProviderAuthManager


class ServiceProviderBoundaryTests(unittest.TestCase):
    def _domain(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        return directory, ProvidersModelsDomain(
            config, auth_manager=ProviderAuthManager(root)
        )

    def test_provider_editor_cannot_create_or_switch_to_login(self) -> None:
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
                    }
                },
            )
            with self.assertRaisesRegex(DomainError, "Service Provider Management"):
                domain.dispatch(
                    "provider.patch",
                    {
                        "provider_id": snapshot["providers"][0]["id"],
                        "changes": {"auth_kind": "claude_login"},
                    },
                )

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
                        "x-litellm-menu-provider-auth": {
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
