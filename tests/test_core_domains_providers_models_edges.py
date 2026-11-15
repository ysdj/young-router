"""Edge cases in the providers and models domain."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from young_router.core.domains.providers_models import ProvidersModelsDomain, _route_key_renamed
from young_router.core.provider_auth import ProviderAuthManager


def _domain(**kwargs):
    directory = tempfile.TemporaryDirectory()
    root = Path(directory.name)
    config = root / "config.yaml"
    config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
    return directory, ProvidersModelsDomain(
        config, auth_manager=ProviderAuthManager(root), **kwargs
    )


class KiloOnlyRuntime:
    """A lane whose only channel is Kilo, with a window and a picker group."""

    def models(self, provider="our-free-model", *, refresh=False):
        return {
            "available": True,
            "detail": "",
            "models": [{"id": "kilo-auto/free", "context_window": 131_072}],
            "routes": {"kilo-group": ["kilo-auto/free"]},
            "channels": {"kilo-auto/free": "kilo"},
            "status": {"provider": provider, "state": "ready", "detail": "", "modelCount": 1},
        }


class ProviderLogoutAllTests(unittest.TestCase):
    def test_signing_every_account_out_detaches_their_routes(self) -> None:
        directory, domain = _domain()
        with directory:
            provider_id = domain.dispatch(
                "service_provider.add", {"kind": "openai_login"}
            )["providers"][0]["id"]
            account_ref = domain._provider_accounts(domain._draft["providers"][0])[0]["ref"]
            domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {
                        "name": "account-route",
                        "upstream_model": "account-route",
                        "account_ref": account_ref,
                        "enabled": True,
                    },
                },
            )
            self.assertEqual(account_ref, domain.draft_state()["providers"][0]["models"][0]["account_ref"])
            domain.dispatch(
                "service_provider.auth_logout", {"provider_id": provider_id}
            )
            model = domain.draft_state()["providers"][0]["models"][0]
            self.assertNotIn("account_ref", model)


class OurFreeModelChannelProjectionTests(unittest.TestCase):
    def test_a_kilo_only_row_reports_its_channel_window_and_group(self) -> None:
        directory, domain = _domain(our_free_model=KiloOnlyRuntime())
        with directory:
            provider = domain.dispatch(
                "service_provider.add",
                {"kind": "our_free_model_login", "channel": "kilo", "name": "Kilo"},
            )["providers"][0]
            route = provider["models"][0]
            self.assertEqual(131_072, route["upstream_context_window"])
            self.assertEqual("kilo-group", route["lane_source"])


class RouteKeyRenameTests(unittest.TestCase):
    def test_a_rename_keeps_a_slashed_model_and_the_segment_separators(self) -> None:
        route_key = "model=openrouter/free / provider=openrouter / upstream=openai/vendor-chat"
        self.assertEqual(
            "model=openrouter/slow / provider=openrouter / upstream=openai/vendor-chat",
            _route_key_renamed(route_key, "openrouter/slow"),
        )


class SafeProviderExtraTests(unittest.TestCase):
    def test_a_malformed_litellm_extra_does_not_break_the_row(self) -> None:
        directory, domain = _domain()
        with directory:
            provider = domain._safe_provider(
                {
                    "name": "edge",
                    "auth_kind": "api_key",
                    "models": [
                        {
                            "model_name": "route",
                            "litellm_model": "openai/route",
                            "litellm_extra": ["rpm=1"],
                        }
                    ],
                },
                0,
            )
        self.assertEqual({}, provider["models"][0]["litellm_extra"])
