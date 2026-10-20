"""WorkBuddy (CN / international) provider integration.

The upstream protocol is a third-party package staged at build time, so these
tests drive the seam instead of the network: a stub runtime stands in for the
worker and asserts what Core asks it for and what the provider domain does with
the answer.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock
import unittest.mock

from young_router.adapters import workbuddy
from young_router.core.domains import DomainError
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.provider_auth import ProviderAuthManager
from young_router.core.service import CoreStore


ROOT = Path(__file__).resolve().parents[1]


CATALOG = [
    {
        "id": "glm-5.3",
        "name": "GLM-5.3",
        "contextWindow": 1_000_000,
        "maxTokens": 64_000,
        "supportsImages": True,
        "billing": {"credits": "x0.79 credits", "free": False},
    },
    {
        "id": "hy3",
        "name": "Hy3",
        "contextWindow": 192_000,
        "maxTokens": 64_000,
        "supportsImages": True,
        "billing": {"credits": "x0.00 credits", "free": True},
    },
]

# The international catalog states a default budget well below the window its
# requests are actually accepted at: a measured 1,048,301-token request is
# answered by the same endpoint that publishes the 300,000 default.
INTERNATIONAL_CATALOG = [
    {
        "id": "deepseek-v4.1-flash",
        "name": "Deepseek-V4.1-Flash",
        "contextWindow": 300_000,
        "defaultContextWindow": 300_000,
        "maxInputTokens": 1_000_000,
        "supportedContextWindows": [300_000, 1_000_000],
        "maxTokens": 128_000,
        "supportsImages": True,
        "billing": {"credits": "x0.00", "free": True},
    },
]


class StubRuntime:
    """The worker surface Core depends on, without a Node process."""

    def __init__(self, *, state: str = "signed-in", available: bool = True) -> None:
        self.state = state
        self.available = available
        self.catalog = CATALOG
        self.calls: list[tuple[str, str]] = []
        self.stopped = 0

    def provider_status(self, provider: str) -> dict[str, object]:
        self.calls.append(("provider_status", provider))
        if not self.available:
            return {}
        return {
            "provider": provider,
            "state": self.state,
            "nickname": "Example User",
            "domain": "www.codebuddy.cn" if provider == "workbuddy" else "www.workbuddy.ai",
            "catalogSource": "live",
            "modelCount": len(CATALOG),
        }

    def status(self, *, refresh: bool = False) -> dict[str, object]:
        self.calls.append(("status", "refresh" if refresh else "read"))
        if not self.available:
            return {"available": False, "detail": "integration_unstaged", "providers": {}}
        return {
            "available": True,
            "providers": {name: self.provider_status(name) for name in workbuddy.WORKBUDDY_PROVIDERS},
        }

    def models(self, provider: str, *, refresh: bool = False) -> dict[str, object]:
        self.calls.append(("models", provider))
        if not self.available:
            return {"available": False, "detail": "integration_unstaged", "models": []}
        if self.state != "signed-in":
            return {"available": False, "detail": "signed out", "models": [], "status": {}}
        return {
            "available": True,
            "source": "live",
            "fetched_at_ms": 1,
            "models": [dict(entry) for entry in self.catalog],
            "status": self.provider_status(provider),
        }

    def environment(self, *, autostart: bool = False) -> dict[str, str]:
        return {
            workbuddy.API_BASE_ENV[workbuddy.WORKBUDDY_PROVIDER]: "http://127.0.0.1:1/workbuddy/v1",
            workbuddy.API_KEY_ENV[workbuddy.WORKBUDDY_PROVIDER]: "local-token",
            workbuddy.API_BASE_ENV[workbuddy.WORKBUDDY_AI_PROVIDER]: "http://127.0.0.1:1/workbuddy-ai/v1",
            workbuddy.API_KEY_ENV[workbuddy.WORKBUDDY_AI_PROVIDER]: "local-token",
        }

    def open_desktop_app(self, provider: str) -> dict[str, object]:
        self.calls.append(("open_desktop_app", provider))
        return {
            "app_name": "WorkBuddy" if provider == "workbuddy" else "WorkBuddy AI",
            "app_path": "/Applications/WorkBuddy.app",
            "opened": True,
        }

    def environment_values(self) -> dict[str, str]:
        return self.environment()

    def stop(self) -> None:
        self.stopped += 1


class WorkBuddyProviderTests(unittest.TestCase):
    def _domain(self, runtime: StubRuntime | None = None):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        domain = ProvidersModelsDomain(
            config,
            auth_manager=ProviderAuthManager(root),
            workbuddy=runtime if runtime is not None else StubRuntime(),
        )
        return directory, domain, config

    def test_service_provider_add_imports_the_live_catalog(self) -> None:
        directory, domain, config = self._domain()
        with directory:
            summary = domain.dispatch(
                "service_provider.add", {"kind": "workbuddy_login"}
            )["operation_summary"]
            self.assertEqual(summary["auth_kind"], "workbuddy_login")
            self.assertEqual(summary["auth_status"], "signed_in")

            provider = domain.snapshot()["providers"][0]
            self.assertEqual(provider["name"], "WorkBuddy")
            self.assertEqual(provider["auth_status"], "signed_in")
            self.assertEqual(provider["api_base"], workbuddy.api_base_reference("workbuddy"))
            self.assertEqual(
                [model["model_name"] for model in provider["models"]],
                ["glm-5.3", "hy3"],
            )
            first = provider["models"][0]
            self.assertEqual(first["litellm_model"], "openai/glm-5.3")
            self.assertEqual(first["upstream_url_surface"], "openai/chat")
            self.assertEqual(first["upstream_protocol_mode"], "fixed")
            self.assertEqual(first["api_key_name"], workbuddy.API_KEY_NAME)
            self.assertEqual(provider["key_states"][0]["name"], workbuddy.API_KEY_NAME)
            self.assertTrue(provider["key_states"][0]["configured"])

            self.assertTrue(domain.validate()["valid"])
            domain.apply()
            document = config.read_text(encoding="utf-8")
            # Only environment references reach the file: the worker port and
            # its bearer stay inside the Core process.
            self.assertIn("os.environ/YOUNG_ROUTER_WORKBUDDY_BASE", document)
            self.assertIn("os.environ/YOUNG_ROUTER_WORKBUDDY_KEY", document)
            self.assertNotIn("127.0.0.1", document)
            self.assertNotIn("local-token", document)
            self.assertIn("model: openai/glm-5.3", document)

    def test_a_route_reports_the_window_the_international_catalog_accepts(self) -> None:
        """The 国际版 window is the accepted one, not the default it asks for.

        The international catalog states a 300,000 default beside a 1,000,000
        ``maxInputTokens`` and a ``supportedContextWindows`` that tops out at
        the same 1,000,000, and a measured 1,048,301-token request is answered
        by that endpoint.  Reporting the default made a route look full at a
        third of what it accepts, so the largest number the entry states is the
        one the row shows.
        """

        runtime = StubRuntime()
        runtime.catalog = INTERNATIONAL_CATALOG
        directory, domain, _config = self._domain(runtime)
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "workbuddy_ai_login"}
            )["providers"][0]
            self.assertEqual(
                1_000_000,
                provider["models"][0]["upstream_context_window"],
            )

    def test_a_catalog_that_states_only_a_default_still_reports_it(self) -> None:
        """An entry with no larger stated number keeps reporting what it does state."""

        runtime = StubRuntime()
        runtime.catalog = [
            {
                "id": "hy3",
                "name": "Hy3",
                "contextWindow": 192_000,
                "defaultContextWindow": 192_000,
                "maxTokens": 64_000,
                "supportsImages": True,
                "billing": {"credits": "x0.00 credits", "free": True},
            }
        ]
        directory, domain, _config = self._domain(runtime)
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "workbuddy_ai_login"}
            )["providers"][0]
            self.assertEqual(
                192_000,
                provider["models"][0]["upstream_context_window"],
            )

    def test_a_wizard_created_entry_starts_without_models(self) -> None:
        """The wizard creates the service entry; the editor links the account."""

        directory, domain, _config = self._domain()
        with directory:
            summary = domain.dispatch(
                "service_provider.add",
                {"kind": "workbuddy_login", "name": "WorkBuddy", "models": []},
            )["operation_summary"]
            self.assertEqual(summary["auth_kind"], "workbuddy_login")
            provider = domain.snapshot()["providers"][0]
            self.assertEqual(provider["models"], [])
            self.assertEqual(provider["api_base"], workbuddy.api_base_reference("workbuddy"))
            self.assertTrue(provider["key_states"][0]["configured"])
            self.assertTrue(domain.validate()["valid"])

            # Linking the account later adds the live roster to that entry.
            added = domain.dispatch(
                "model.add_many",
                {
                    "provider_id": provider["id"],
                    "models": [
                        {"name": "glm-5.3", "upstream_model": "glm-5.3", "enabled": True, "order": 1},
                        {"name": "hy3", "upstream_model": "hy3", "enabled": True, "order": 2},
                    ],
                },
            )["providers"][0]
            self.assertEqual([model["model_name"] for model in added["models"]], ["glm-5.3", "hy3"])
            self.assertTrue(all(model["upstream_protocol_mode"] == "fixed" for model in added["models"]))

    def test_a_route_the_user_re_protocolled_survives_the_next_model_add(self) -> None:
        """Growing the roster never rewrites a route's own protocol choice.

        The pane offers 协议方式 and the backup protocol on *every* route, so the
        user can move one off the account's default.  Adding a model runs over
        the provider's whole model list, and a wholesale assignment there
        silently re-pinned every route each time the roster grew — the one
        field the user had just edited reverted, and which surface a route
        speaks is what decides whether a client is bridged or refused.
        """

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch("service_provider.add", {"kind": "workbuddy_login"})["providers"][0]
            provider_id = provider["id"]
            model_id = provider["models"][0]["id"]
            self.assertEqual(provider["models"][0]["upstream_protocol_mode"], "fixed")

            domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "changes": {"upstream_protocol_mode": "fallback"},
                },
            )
            self.assertEqual(
                domain.snapshot()["providers"][0]["models"][0]["upstream_protocol_mode"],
                "fallback",
            )

            domain.dispatch(
                "model.add",
                {"provider_id": provider_id, "model": {"name": "extra", "upstream_model": "extra"}},
            )
            models = domain.snapshot()["providers"][0]["models"]
            self.assertEqual(
                [model["upstream_protocol_mode"] for model in models],
                ["fallback", "fixed", "fixed"],
            )
            # The account's own surface is still what every route starts on.
            self.assertTrue(all(model["upstream_url_surface"] == "openai/chat" for model in models))

    def test_a_workbuddy_route_can_follow_the_rate_its_own_catalog_states(self) -> None:
        """跟随倍率 is not reserved for relay keys.

        The desktop catalog already states what each model bills at
        (``x0.79``), which is the same relation a station's group multiplier
        is: the route's order follows a number this app does not decide.  The
        account publishes it on a catalog read, so the order materializes from
        that number instead of waiting for a station read — and the write has
        to survive the round trip through the config file, since `order` is what
        the router actually sorts by.
        """

        directory, domain, config = self._domain()
        with directory:
            provider = domain.dispatch("service_provider.add", {"kind": "workbuddy_login"})["providers"][0]
            provider_id = provider["id"]
            # The catalog read is where the account states its rates.
            domain.dispatch("workbuddy_models", {"provider": "workbuddy"})
            self.assertEqual(domain._published_rates, {"workbuddy": {"glm-5.3": 0.79, "hy3": 0.0}})

            model_id = provider["models"][0]["id"]
            domain.dispatch(
                "model.patch",
                {"provider_id": provider_id, "model_id": model_id, "changes": {"order_mode": "relay_multiplier"}},
            )
            followed = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual(followed["order_mode"], "relay_multiplier")
            self.assertEqual(followed["effective_order"], 0.79)
            self.assertEqual(followed["order"], 0.79)
            self.assertTrue(domain.validate()["valid"])

            self.assertTrue(domain.apply()["applied"])
            document = config.read_text(encoding="utf-8")
            self.assertIn("x-young-router-order-mode: relay_multiplier", document)
            self.assertIn("order: 0.79", document)

            # The mode and the number it resolved to both come back.
            domain.reload()
            reloaded = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual(reloaded["order_mode"], "relay_multiplier")
            self.assertEqual(reloaded["effective_order"], 0.79)

    def test_following_the_rate_learns_it_before_the_pane_ever_shows_it(self) -> None:
        """The rate arrives when it is asked for, not only when a pane read it.

        A route can be told to follow its rate before any catalog read happened
        — a link created straight from the provider editor, or a session that
        never opened the model list.  The edit is the moment the number is
        needed, so it is learned there; otherwise the mode was accepted and the
        write then failed with a null the dumper could not order by.
        """

        directory, domain, config = self._domain()
        with directory:
            provider = domain.dispatch("service_provider.add", {"kind": "workbuddy_login"})["providers"][0]
            self.assertEqual(domain._published_rates, {})
            domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider["id"],
                    "model_id": provider["models"][0]["id"],
                    "changes": {"order_mode": "relay_multiplier"},
                },
            )
            followed = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual(followed["effective_order"], 0.79)
            self.assertTrue(domain.validate()["valid"])
            self.assertTrue(domain.apply()["applied"])
            self.assertIn("order: 0.79", config.read_text(encoding="utf-8"))

    def test_a_plain_key_route_cannot_follow_a_rate_nobody_states(self) -> None:
        """Following a rate needs an authority that states one.

        A plain key authenticates any surface but states no rate, so a route on
        one cannot follow a number that does not exist.  The refusal keeps its
        own message instead of silently writing a hand-typed order.
        """

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "Custom",
                        "api_base": "https://api.example.test/v1",
                        "models": [{"name": "m1", "upstream_model": "m1", "enabled": True}],
                    }
                },
            )["providers"][0]
            with self.assertRaisesRegex(DomainError, "publishes one"):
                domain.dispatch(
                    "model.patch",
                    {
                        "provider_id": provider["id"],
                        "model_id": provider["models"][0]["id"],
                        "changes": {"order_mode": "relay_multiplier"},
                    },
                )

    def test_service_provider_add_refuses_a_signed_out_desktop_app(self) -> None:
        directory, domain, _config = self._domain(StubRuntime(state="signed-out"))
        with directory:
            with self.assertRaisesRegex(DomainError, "WorkBuddy desktop app"):
                domain.dispatch("service_provider.add", {"kind": "workbuddy_login"})

    def test_provider_status_reports_an_unstaged_integration_as_unsupported(self) -> None:
        directory, domain, _config = self._domain(StubRuntime(available=False))
        with directory:
            provider = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "Manual",
                        "api_base": "https://api.example.test/v1",
                        "models": [],
                    }
                },
            )["providers"][0]
            self.assertEqual(provider["auth_status"], "signed_out")

    def test_workbuddy_status_and_models_answer_the_wizard(self) -> None:
        runtime = StubRuntime()
        directory, domain, _config = self._domain(runtime)
        with directory:
            status = domain.dispatch("workbuddy_status", {"refresh": True})["operation_summary"]
            self.assertEqual(status["operation"], "workbuddy_status")
            self.assertTrue(status["available"])
            self.assertEqual(status["providers"]["workbuddy"]["nickname"], "Example User")
            self.assertIn(("status", "refresh"), runtime.calls)

            models = domain.dispatch(
                "workbuddy_models", {"provider": "workbuddy"}
            )["operation_summary"]
            self.assertEqual(models["operation"], "workbuddy_models")
            self.assertEqual(models["provider"], "workbuddy")
            self.assertTrue(models["available"])
            self.assertEqual([entry["id"] for entry in models["models"]], ["glm-5.3", "hy3"])

            ai = domain.dispatch(
                "workbuddy_models", {"provider": "workbuddy-ai"}
            )["operation_summary"]
            self.assertEqual(ai["provider"], "workbuddy-ai")
            self.assertIn(("models", "workbuddy-ai"), runtime.calls)

    def test_workbuddy_models_reject_an_unknown_provider(self) -> None:
        directory, domain, _config = self._domain()
        with directory:
            with self.assertRaises(DomainError):
                domain.dispatch("workbuddy_models", {"provider": "codebuddy"})

    def test_selected_models_are_the_only_ones_added(self) -> None:
        directory, domain, _config = self._domain()
        with directory:
            domain.dispatch(
                "service_provider.add",
                {
                    "kind": "workbuddy_ai_login",
                    "name": "WorkBuddy AI",
                    "models": [{"name": "hy3", "upstream_model": "hy3", "enabled": True, "order": 1}],
                },
            )
            provider = domain.snapshot()["providers"][0]
            self.assertEqual(provider["auth_kind"], "workbuddy_ai_login")
            self.assertEqual([model["model_name"] for model in provider["models"]], ["hy3"])
            self.assertEqual(provider["api_base"], workbuddy.api_base_reference("workbuddy-ai"))

    def test_models_added_later_inherit_the_account_route(self) -> None:
        directory, domain, _config = self._domain()
        with directory:
            dispatch = domain.dispatch(
                "service_provider.add",
                {
                    "kind": "workbuddy_login",
                    "models": [{"name": "hy3", "upstream_model": "hy3", "enabled": True, "order": 1}],
                },
            )
            provider_id = dispatch["providers"][0]["id"]
            next_state = domain.dispatch(
                "model.add_many",
                {"provider_id": provider_id, "models": [{"name": "glm-5.3", "upstream_model": "glm-5.3"}]},
            )
            added = next_state["providers"][0]["models"][-1]
            self.assertEqual(added["model_name"], "glm-5.3")
            self.assertEqual(added["upstream_url_surface"], "openai/chat")
            self.assertEqual(added["upstream_protocol_mode"], "fixed")
            self.assertEqual(added["api_key_name"], workbuddy.API_KEY_NAME)

    def test_workbuddy_providers_are_managed_through_the_service_surface(self) -> None:
        directory, domain, _config = self._domain()
        with directory:
            dispatch = domain.dispatch("service_provider.add", {"kind": "workbuddy_login"})
            provider_id = dispatch["providers"][0]["id"]
            with self.assertRaisesRegex(DomainError, "Service Provider Management"):
                domain.dispatch("provider.patch", {"provider_id": provider_id, "changes": {"name": "Other"}})
            renamed = domain.dispatch(
                "service_provider.patch",
                {"provider_id": provider_id, "provider": {"name": "WorkBuddy CN"}},
            )
            self.assertEqual(renamed["providers"][0]["name"], "WorkBuddy CN")

    def test_a_workbuddy_provider_key_and_endpoint_are_not_editable(self) -> None:
        directory, domain, _config = self._domain()
        with directory:
            dispatch = domain.dispatch("service_provider.add", {"kind": "workbuddy_login"})
            provider_id = dispatch["providers"][0]["id"]
            with self.assertRaises(DomainError):
                domain.dispatch(
                    "service_provider.patch",
                    {"provider_id": provider_id, "provider": {"api_base": "https://elsewhere.test/v1"}},
                )

    def test_a_workbuddy_providers_type_is_not_switchable(self) -> None:
        """The type is what the provider is, not a field it adopts.

        Both workbuddy kinds address *different* loopback workers with different
        key slots, so a switch between them is a different provider, not a
        rename — and the pane states the type read-only.  The patch is refused,
        and the address, the key slot, and every route stay where they were.
        """

        directory, domain, _config = self._domain()
        with directory:
            added = domain.dispatch("service_provider.add", {"kind": "workbuddy_login"})
            provider_id = added["providers"][0]["id"]
            before = {
                key: added["providers"][0][key]
                for key in ("api_base", "api_key_names", "auth_kind")
            }
            with self.assertRaisesRegex(DomainError, "type is fixed"):
                domain.dispatch(
                    "service_provider.patch",
                    {"provider_id": provider_id, "provider": {"auth_kind": "workbuddy_ai_login"}},
                )
            provider = domain.snapshot()["providers"][0]
            self.assertEqual(provider["auth_kind"], "workbuddy_login")
            self.assertEqual(provider["api_base"], before["api_base"])
            self.assertEqual(provider["api_key_names"], before["api_key_names"])
            self.assertEqual(
                provider["extra"]["x-young-router-provider-auth"]["kind"], "workbuddy_login"
            )
            self.assertEqual(
                provider["models"][0]["upstream_url_surface"] if provider["models"] else "openai/chat",
                "openai/chat",
            )

    def test_a_live_read_never_advances_the_shared_revision(self) -> None:
        """A read that stages nothing is not a settings change.

        Every window keys its debounced Apply on Core's shared revision, so a
        read that bumped it made the next Apply reconcile against a change the
        user never made -- the pane reported its own commit back as "settings
        changed outside this window".  A WorkBuddy entry reads the account and
        the catalog on every visit, which is exactly the sequence that produced
        that message; a read must answer from its own ``action_summary`` while
        leaving the revision alone, and a real edit must still advance it.
        """

        runtime = StubRuntime()
        directory, domain, config = self._domain(runtime)
        with directory:
            core = CoreStore(
                metadata_path=Path(directory.name) / ".litellm-runtime" / "core-state.json",
                domains=[domain],
            )
            core.dispatch(
                {"domain": "providers_models", "type": "service_provider.add", "payload": {"kind": "workbuddy_login", "name": "wb", "models": []}},
                expected_revision=core.revision,
            )
            revision = core.revision
            # The pane's entry read, then the re-read a cold worker triggers:
            # both must be accepted at the revision the window already holds.
            for _ in range(2):
                result = core.dispatch(
                    {"domain": "providers_models", "type": "workbuddy_status", "payload": {"refresh": True}},
                    expected_revision=revision,
                )
                self.assertEqual(revision, result["revision"])
                self.assertEqual("workbuddy_status", result["action_summary"]["operation"])
            # The other two live reads answer the same way.
            for action, payload in (
                ("workbuddy_models", {"provider": "workbuddy"}),
                ("workbuddy_login", {"provider": "workbuddy"}),
            ):
                result = core.dispatch(
                    {"domain": "providers_models", "type": action, "payload": payload},
                    expected_revision=revision,
                )
                self.assertEqual(revision, result["revision"], action)
            # A real edit is still a state change, and a repeated no-op is not.
            provider_id = domain.snapshot()["providers"][0]["editor_id"]
            edited = core.dispatch(
                {"domain": "providers_models", "type": "service_provider.patch", "payload": {"provider_id": provider_id, "provider": {"enabled": False}}},
                expected_revision=revision,
            )
            self.assertGreater(edited["revision"], revision)
            again = core.dispatch(
                {"domain": "providers_models", "type": "service_provider.patch", "payload": {"provider_id": provider_id, "provider": {"enabled": False}}},
                expected_revision=edited["revision"],
            )
            self.assertEqual(edited["revision"], again["revision"])

    def test_a_service_account_read_answers_the_window_then_reads_for_real(self) -> None:
        """Entering a WorkBuddy provider repeatedly must not re-read the account.

        Every entry into the pane reads the desktop-app account and its credit,
        and each read can start a worker and ask upstream, so 运行时 publishes
        one window that answers a later entry -- the same contract the relay
        account pane has.  ``refresh`` is the section's own 刷新 button and
        always reads for real, and a window of 0 re-reads on every entry.
        """

        runtime = StubRuntime()
        directory, domain, _config = self._domain(runtime)
        with directory:
            reads = lambda: [call for call in runtime.calls if call[0] == "status"]
            domain.dispatch("workbuddy_status", {"refresh": True})
            self.assertEqual(1, len(reads()))
            # An implicit entry inside the window is answered, not re-read.
            summary = domain.dispatch("workbuddy_status", {"refresh": False})["operation_summary"]
            self.assertEqual(1, len(reads()))
            self.assertIs(True, summary["remembered"])
            self.assertEqual("Example User", summary["providers"]["workbuddy"]["nickname"])
            # 刷新 always reaches the worker.
            domain.dispatch("workbuddy_status", {"refresh": True})
            self.assertEqual(2, len(reads()))
            # A window of 0 means a read on every entry.
            settings = Path(directory.name) / "runtime-settings.env"
            settings.write_text("YOUNG_ROUTER_SERVICE_ACCOUNT_RECHECK_SECONDS=0\n", encoding="utf-8")
            with mock.patch.object(
                domain, "_workbuddy_recheck_seconds", return_value=0.0,
            ):
                domain.dispatch("workbuddy_status", {"refresh": False})
            self.assertEqual(3, len(reads()))

    def test_the_projection_publishes_the_window_and_the_observation_age(self) -> None:
        """The pane applies Core's own window instead of deciding by itself."""

        runtime = StubRuntime()
        directory, domain, _config = self._domain(runtime)
        with directory:
            self.assertEqual(60.0, domain.snapshot()["service_account_recheck_seconds"])
            domain.dispatch("service_provider.add", {"kind": "workbuddy_login", "models": []})
            provider = domain.snapshot()["providers"][0]
            self.assertIsInstance(provider["auth_observed_seconds_ago"], float)
            self.assertLess(provider["auth_observed_seconds_ago"], 5.0)
            # A provider that was never read states no age, so the pane cannot
            # mistake "nothing observed" for "observed just now".
            domain._workbuddy_observed.clear()
            self.assertIsNone(domain.snapshot()["providers"][0]["auth_observed_seconds_ago"])

    def test_a_snapshot_never_calls_the_live_worker(self) -> None:
        """A projection must not pay for a WorkBuddy account read.

        ``CoreStore.snapshot()`` holds the store lock for its whole call, and
        the provider table is projected on it, so a cold worker (a 45 s start,
        a desktop credential read, an upstream catalog and credit fetch) froze
        every window in the app — and a write could not even reach Core to say
        so.  A snapshot therefore reports the last state a live read observed.
        """

        runtime = StubRuntime()
        directory, domain, _config = self._domain(runtime)
        with directory:
            domain.dispatch("service_provider.add", {"kind": "workbuddy_login", "models": []})
            # The create is the one place that legitimately asks the worker.
            self.assertTrue(runtime.calls)
            before = len(runtime.calls)
            for _ in range(3):
                provider = domain.snapshot()["providers"][0]
                self.assertEqual(provider["auth_status"], "signed_in")
                self.assertEqual(provider["auth_observed"]["nickname"], "Example User")
            self.assertEqual(len(runtime.calls), before)

    def test_a_wizard_created_entry_takes_its_state_from_the_create(self) -> None:
        """Nothing has observed the account yet, so nothing may guess it."""

        directory, domain, _config = self._domain()
        with directory:
            summary = domain.dispatch(
                "service_provider.add",
                {"kind": "workbuddy_login", "name": "WorkBuddy", "models": []},
            )["operation_summary"]
            self.assertEqual(summary["auth_status"], "signed_in")
            self.assertEqual(domain.snapshot()["providers"][0]["auth_status"], "signed_in")

    def test_a_snapshot_states_nothing_it_has_not_observed(self) -> None:
        """A signed-out projection is not a stale success."""

        runtime = StubRuntime(state="signed-out")
        directory, domain, _config = self._domain(runtime)
        with directory:
            domain.dispatch("workbuddy_status", {"refresh": True})
            self.assertEqual(domain.snapshot()["providers"], [])

    def test_a_read_that_never_observed_the_worker_stays_unstated(self) -> None:
        """With no live read, Core says nothing rather than guessing."""

        class Unreachable(StubRuntime):
            def provider_status(self, provider: str) -> dict[str, object]:
                raise workbuddy.WorkBuddyUnavailable("the worker did not answer")

            def status(self, *, refresh: bool = False) -> dict[str, object]:
                raise workbuddy.WorkBuddyUnavailable("the worker did not answer")

        directory, domain, _config = self._domain(Unreachable())
        with directory:
            domain.dispatch("service_provider.add", {"kind": "workbuddy_login", "models": []})
            # A replacement Core, a rollback, or a worker that has never been
            # reached: nothing has observed the account, so the projection says
            # so rather than inventing a success or reaching for the worker.
            domain._workbuddy_observed.clear()
            safe = domain.snapshot()["providers"][0]
            # An answer nobody observed is never reported as a success, and it
            # never reaches for the worker to invent one.
            self.assertNotEqual(safe["auth_status"], "signed_in")
            self.assertIs(safe["auth_configured"], False)
            self.assertEqual(safe["auth_observed"], {})
            self.assertIn(safe["auth_status"], {"signed_out", "unsupported"})

    def test_the_login_action_opens_the_desktop_app(self) -> None:
        runtime = StubRuntime()
        directory, domain, _config = self._domain(runtime)
        with directory:
            summary = domain.dispatch("workbuddy_login", {"provider": "workbuddy"})[
                "operation_summary"
            ]
            self.assertEqual(summary["app_name"], "WorkBuddy")
            self.assertIs(summary["opened"], True)
            self.assertIn(("open_desktop_app", "workbuddy"), runtime.calls)


class WorkBuddyServiceEnvironmentTests(unittest.TestCase):
    """The proxy child resolves the worker loopback from its own environment."""

    def test_the_worker_shapes_every_streamed_delta_before_a_client_sees_it(self) -> None:
        """A reasoning frame must not claim the answer started.

        The shim states every delta field it knows, so a reasoning frame also
        carries ``content: ""``; a client reads that as "the answer started" and
        closes the thinking block it just opened, which turned one thought into
        one collapsed 深度思考 row per fragment.  The worker therefore passes
        every streamed line through the shaper, which drops the empty fields and
        leaves every other frame byte-for-byte.
        """

        root = ROOT
        worker = (root / "young_router/adapters/workbuddy_worker.mjs").read_text(encoding="utf-8")
        shaper = (root / "young_router/adapters/workbuddy_stream.mjs").read_text(encoding="utf-8")
        self.assertIn("import { normalizeChatCompletionStreamLine } from './workbuddy_stream.mjs'", worker)
        self.assertIn("res.write(normalizeChatCompletionStreamLine(buffer.slice(0, newline + 1)))", worker)
        self.assertIn("if (buffer) res.write(normalizeChatCompletionStreamLine(buffer))", worker)
        self.assertNotIn("readable.pipe(res)", worker)
        for field in ("'content'", "'refusal'", "'reasoning_content'", "'tool_calls'", "'function_call'"):
            self.assertIn(field, shaper)
        # The content-free legacy function_call mirror is dropped too: LiteLLM's
        # stream-chunk builder reads any present function_call as a legacy call
        # and fails on the dict shape, so an empty mirror used to turn an
        # answered stream into an upstream route failure.
        self.assertIn("function isEmptyLegacyFunctionCall(value)", shaper)
        self.assertIn("field === 'function_call'", shaper)
        self.assertIn("return isEmptyFieldValue(name) && (args === undefined || isEmptyFieldValue(args))", shaper)
        self.assertIn("if (!trimmed.startsWith('data:')) return line", shaper)
        self.assertIn("if (!data || data === '[DONE]') return line", shaper)

    def test_only_a_configured_route_starts_the_worker(self) -> None:
        from young_router.core.operations import CoreServiceController

        root = Path(tempfile.mkdtemp())
        controller = CoreServiceController(root)
        runtime = StubRuntime()
        controller.attach_workbuddy(runtime)

        # A status read publishes nothing and starts nothing.
        self.assertEqual(controller._workbuddy_environment(autostart=False), runtime.environment())
        self.assertEqual(runtime.stopped, 0)

        # With no WorkBuddy route in the staged config, a proxy launch releases
        # the worker instead of leaving a credential-holding process behind.
        self.assertEqual(controller._workbuddy_environment(autostart=True), {})
        self.assertEqual(runtime.stopped, 1)

        controller.paths.runtime_config.parent.mkdir(parents=True, exist_ok=True)
        controller.paths.runtime_config.write_text(
            'model_list:\n  - litellm_params:\n      api_base: os.environ/YOUNG_ROUTER_WORKBUDDY_BASE\n',
            encoding="utf-8",
        )
        environment = controller._workbuddy_environment(autostart=True)
        self.assertEqual(environment["YOUNG_ROUTER_WORKBUDDY_BASE"], "http://127.0.0.1:1/workbuddy/v1")
        self.assertEqual(environment["YOUNG_ROUTER_WORKBUDDY_KEY"], "local-token")
        self.assertEqual(environment["YOUNG_ROUTER_WORKBUDDY_AI_BASE"], "http://127.0.0.1:1/workbuddy-ai/v1")

    def test_a_missing_worker_never_blocks_the_proxy_environment(self) -> None:
        from young_router.core.operations import CoreServiceController

        class FailingRuntime(StubRuntime):
            def environment(self, *, autostart: bool = False) -> dict[str, str]:
                raise workbuddy.WorkBuddyUnavailable("no node")

        root = Path(tempfile.mkdtemp())
        controller = CoreServiceController(root)
        controller.attach_workbuddy(FailingRuntime())
        controller.paths.runtime_config.parent.mkdir(parents=True, exist_ok=True)
        controller.paths.runtime_config.write_text(
            "model_list:\n  - api_base: os.environ/YOUNG_ROUTER_WORKBUDDY_BASE\n", encoding="utf-8"
        )
        self.assertEqual(controller._workbuddy_environment(autostart=True), {})


class WorkBuddyDesktopCredentialTests(unittest.TestCase):
    """A sealed desktop credential must open for the product it belongs to.

    Both desktop apps seal their token fields behind a key only that product's
    own Electron binary can produce (5.6-era at-rest protection), so the store
    can read a credential only through the library's per-variant key provider.
    The worker used to build its stores without one, which left the provider's
    own default (``discovery: 'none'``) in place: ``provider_status`` then
    answered ``electron-binary-unavailable`` no matter how well the user was
    signed in, so the pane showed 未登录 beside a 刷新 button that could never
    change it.  The library ships that factory in its internal chunk rather
    than from its entry point, so these tests drive the real worker against a
    small stub library that reproduces the published shape.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("Node.js is not available on this machine")

    # The stub library: the entry point exports the surface the worker imports,
    # and only the internal sibling chunk carries the key factory -- exactly the
    # arrangement that made the worker's own wiring invisible.
    _ENTRY = """\
export class WorkBuddyUpstreamClient {
  async refreshToken() { return {} }
}
export class WorkBuddyCatalog {
  constructor() { this.models = [] }
  current() { return this.models }
  setVisible() {}
  set(models) { this.models = models }
}
export class WorkBuddyCatalogStore {
  get() { return undefined }
  set() {}
}
export function createWorkBuddyShim() {
  return { ready: Promise.resolve(), baseUrl: () => 'http://127.0.0.1:1', token: () => 'shim', close: () => {} }
}
export const WORKBUDDY_VARIANTS = [
  { id: 'workbuddy', displayName: 'WorkBuddy', appName: 'WorkBuddy', region: 'cn',
    electron: { macOS: { defaultPath: '/Applications/WorkBuddy.app/Contents/MacOS/Electron' } } },
  { id: 'workbuddy-ai', displayName: 'WorkBuddy AI', appName: 'WorkBuddy AI', region: 'global',
    electron: { macOS: { defaultPath: '/Applications/WorkBuddy AI.app/Contents/MacOS/Electron' } } },
]
export function readBundleVersion() { return '9.9.9' }
export { WorkBuddyCredentialStore } from './chunk.js'
"""

    _CHUNK = """\
export class WorkBuddyCredentialStore {
  constructor(options) {
    this.variant = options.variant
    this.keyProvider = options.keyProvider
  }
  // What the store was built with is the whole question: a store without the
  // library's key provider cannot open a sealed document, and says so.
  async status() {
    if (this.keyProvider === undefined) return { state: 'signed-out', reasonCode: 'electron-binary-unavailable' }
    return { state: 'signed-in', reasonCode: 'wired', helper: this.keyProvider.helperPath() }
  }
  async resolve() { throw new Error('the stub serves no credential') }
}
"""

    # The factory this build publishes, in the shape the worker resolves: a
    # named function export on the internal chunk beside ``index.js``.
    _FACTORY = """\
export function atRestKeyProviderFor(variant) {
  return { helperPath: () => `/apps/${variant.id}` }
}
"""

    def _library(self, root: Path, *, expose_factory: bool) -> Path:
        """The stub library; ``expose_factory`` publishes the key factory."""

        library_root = root / "library"
        library_root.mkdir(parents=True)
        chunk = self._CHUNK + (self._FACTORY if expose_factory else "")
        (library_root / "index.js").write_text(self._ENTRY, encoding="utf-8")
        (library_root / "chunk.js").write_text(chunk, encoding="utf-8")
        return library_root

    def _status(self, library_root: Path, root: Path) -> dict:
        """Start the real worker against one stub library and read its status."""

        import json
        import subprocess
        import urllib.request

        worker = ROOT / "young_router" / "adapters" / "workbuddy_worker.mjs"
        token = "stub-token"
        process = subprocess.Popen(
            [
                self.node,
                str(worker),
                "--port", "0",
                "--token", token,
                "--root", str(root),
                "--entry", str(library_root / "index.js"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # ``kill`` is registered after ``wait`` on purpose: cleanups run
        # last-in-first-out, so this kills the worker and only then reaps it.
        # The other order waits on a live process and hangs the suite.  The
        # pipes are closed first, because a killed worker never closes them.
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stderr.close)
        line = process.stdout.readline()
        if not line:
            # Read stderr only now, and never inside the assertion's own
            # message: a worker that is still serving holds this pipe open, so
            # an eager read would wait forever on the very failure it describes.
            self.fail(f"the worker never announced itself: {process.stderr.read()}")
        ready = json.loads(line)
        self.assertIs(ready.get("ready"), True)
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(ready['port'])}/control/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_the_store_is_wired_with_the_librarys_own_key_provider(self) -> None:
        """Both products get their own key provider, from the library."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = self._status(self._library(root, expose_factory=True), root / "worker")

        providers = document["providers"]
        for provider_id in ("workbuddy", "workbuddy-ai"):
            entry = providers[provider_id]
            self.assertEqual(
                entry["state"],
                "signed-in",
                f"{provider_id} was not given the library's key provider: {entry}",
            )
            self.assertEqual(entry["reasonCode"], "wired")
            # Each variant resolved its *own* helper, not the other product's.
            self.assertEqual(entry["helper"], f"/apps/{provider_id}")

    def test_a_library_without_the_factory_is_reported_rather_than_guessed(self) -> None:
        """A build that drops the factory degrades to a diagnosis.

        The worker must not guess an Electron path of its own: resolving which
        app is installed is the library's product knowledge, and 未登录 with its
        reason is the honest answer when that name is absent -- the same answer
        the library's own default gives.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = self._status(self._library(root, expose_factory=False), root / "worker")

        self.assertIs(document["ready"], True)
        for provider_id in ("workbuddy", "workbuddy-ai"):
            self.assertEqual(
                document["providers"][provider_id]["reasonCode"],
                "electron-binary-unavailable",
            )

    def test_the_worker_names_the_factory_and_embeds_no_product_path(self) -> None:
        """The seam is the library's published name, not a path copied here."""

        worker = (ROOT / "young_router" / "adapters" / "workbuddy_worker.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("atRestKeyProviderFor", worker)
        self.assertIn("keyProvider", worker)
        # Resolving which app is installed stays upstream's knowledge.
        self.assertNotIn("/Applications/WorkBuddy", worker)
        self.assertNotIn("WORKBUDDY_AI_ELECTRON_BIN", worker)

    def test_each_product_reports_its_own_desktop_app(self) -> None:
        """One product's 去登录 must not open the other product's app.

        The library's ``installedAppVersion`` reads the international App alone
        -- it takes no variant -- so the host was handed the WorkBuddy AI bundle
        for a WorkBuddy account, and the Core's ``open <bundle>`` then launched
        the wrong product for that account's sign-in.
        """

        worker = (ROOT / "young_router" / "adapters" / "workbuddy_worker.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("installedDesktopApp", worker)
        self.assertNotIn("library.installedAppVersion(", worker)
        # The bundle comes from the descriptor each product publishes.
        self.assertIn("variant.electron?.macOS?.defaultPath", worker)


class WorkBuddyModuleTests(unittest.TestCase):
    def test_core_resolves_the_references_it_owns(self) -> None:
        """A Core-side read sees the same loopback base and bearer as the child."""

        import os

        from young_router.adapters import workbuddy as workbuddy_module
        from young_router.core.domains.providers_models import ProvidersModelsDomain as D

        names = [
            workbuddy_module.API_BASE_ENV["workbuddy"],
            workbuddy_module.API_KEY_ENV["workbuddy"],
        ]
        saved = {name: os.environ.pop(name, None) for name in names}
        try:
            workbuddy_module._publish({names[0]: "http://127.0.0.1:1/workbuddy/v1", names[1]: "token"})
            for name in names:
                os.environ.pop(name, None)
            provider = {"api_base": f"os.environ/{names[0]}", "api_keys": [{"name": "workbuddy-local", "value": f"os.environ/{names[1]}"}]}
            self.assertEqual(D._provider_api_base(provider), "http://127.0.0.1:1/workbuddy/v1")
            self.assertEqual(D._provider_credential(provider), ("workbuddy-local", "token"))
        finally:
            for name, value in saved.items():
                os.environ.pop(name, None)
                if value is not None:
                    os.environ[name] = value

    def test_a_models_own_reference_resolves_to_its_service(self) -> None:
        """A model carrying the loopback reference is probed on that address."""

        import os

        from young_router.adapters import workbuddy as workbuddy_module
        from young_router.core.domains.providers_models import ProvidersModelsDomain as D

        name = workbuddy_module.API_BASE_ENV["workbuddy"]
        saved = os.environ.pop(name, None)
        try:
            workbuddy_module._publish({name: "http://127.0.0.1:2/workbuddy/v1"})
            os.environ.pop(name, None)
            model = {"api_base": f"os.environ/{name}", "upstream_url_surface": "openai/chat"}
            self.assertEqual(D._model_api_base({}, model), "http://127.0.0.1:2/workbuddy/v1")
            self.assertEqual(
                D._probe_surfaces(D._model_api_base({}, model), model), ["openai/chat"]
            )
        finally:
            os.environ.pop(name, None)
            if saved is not None:
                os.environ[name] = saved

    def test_a_managed_route_is_probed_on_its_own_surface(self) -> None:
        """The worker mounts one surface, so that is the one a probe tests."""

        from young_router.adapters import workbuddy as workbuddy_module
        from young_router.core.domains.providers_models import ProvidersModelsDomain as D

        reference = workbuddy_module.api_base_reference("workbuddy")
        model = {"upstream_url_surface": "openai/chat", "upstream_protocol_mode": "fixed"}
        provider = {"api_base": reference}
        self.assertEqual(
            D._probe_surfaces("http://127.0.0.1:9931/workbuddy/v1", model, provider),
            ["openai/chat"],
        )
        # A model carrying the provider's own reference answers the same way.
        self.assertEqual(
            D._probe_surfaces(
                "http://localhost:9931/workbuddy/v1",
                {**model, "api_base": reference},
            ),
            ["openai/chat"],
        )
        # A service the user runs is asked about every protocol it might
        # speak, loopback or not: a local server that only serves Chat
        # Completions has to be discoverable.
        for address in ("http://127.0.0.1:11434/v1", "http://localhost:9931/v1"):
            self.assertEqual(
                sorted(D._probe_surfaces(address, model)),
                ["anthropic", "openai/chat", "openai/responses"],
            )
        self.assertEqual(
            sorted(D._probe_surfaces("https://api.example.test/v1", model)),
            ["anthropic", "openai/chat", "openai/responses"],
        )

    def test_the_managed_registry_names_no_particular_integration(self) -> None:
        """\u201cA service this deployment runs itself\u201d is a registry, not a product.

        The rule that keeps a probe off this app's own plumbing is about the
        deployment owning the worker, so the addresses come from the adapters
        that publish one.  A product name here would mean the next bundled
        worker silently gets probed as if it were a third party's server.
        """

        import young_router.adapters as adapters
        from young_router.adapters import our_free_model as our_free_model_module
        from young_router.adapters import workbuddy as workbuddy_module

        # Every bundled worker that serves a model endpoint publishes its own
        # reference, and the registry is their union -- naming one product here
        # would be exactly the coupling this rule exists to prevent.
        expected = frozenset(
            f"os.environ/{name}"
            for module in (workbuddy_module, our_free_model_module)
            for name in module.API_BASE_ENV.values()
        )
        self.assertEqual(adapters.managed_base_references(), expected)
        # The resolved half of the same fact is asked for by address, so a
        # worker that publishes one is recognized without naming it here.
        self.assertIsInstance(adapters.managed_base_urls(), tuple)

    def test_a_remembered_worker_publishes_its_environment(self) -> None:
        """Core resolves the loopback base and bearer before it starts one."""

        import os

        from young_router.adapters import workbuddy as workbuddy_module

        saved = {name: os.environ.get(name) for name in workbuddy_module.API_BASE_ENV.values()}

        root = Path(tempfile.mkdtemp())
        state = root / ".litellm-runtime" / "workbuddy"
        state.mkdir(parents=True)
        (state / "worker.json").write_text('{"port": 45454, "token": "remembered"}', encoding="utf-8")
        try:
            with mock.patch.object(workbuddy_module, "available", lambda: True):
                runtime = workbuddy_module.WorkBuddyRuntime(root)
            self.assertEqual(
                runtime.environment_values()[workbuddy_module.API_BASE_ENV["workbuddy"]],
                "http://127.0.0.1:45454/workbuddy/v1",
            )
            self.assertEqual(
                os.environ[workbuddy_module.API_KEY_ENV["workbuddy"]], "remembered"
            )
        finally:
            for name in workbuddy_module.API_BASE_ENV.values():
                os.environ.pop(name, None)
            for name in workbuddy_module.API_KEY_ENV.values():
                os.environ.pop(name, None)
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

    def test_references_are_environment_names_only(self) -> None:
        self.assertEqual(
            workbuddy.api_base_reference("workbuddy"), "os.environ/YOUNG_ROUTER_WORKBUDDY_BASE"
        )
        self.assertEqual(
            workbuddy.api_key_reference("workbuddy-ai"), "os.environ/YOUNG_ROUTER_WORKBUDDY_AI_KEY"
        )
        with self.assertRaises(ValueError):
            workbuddy.api_base_reference("codebuddy")

    def test_auth_kind_mapping_covers_both_products(self) -> None:
        self.assertEqual(
            workbuddy.AUTH_KIND_TO_PROVIDER["workbuddy_login"], "workbuddy"
        )
        self.assertEqual(
            workbuddy.AUTH_KIND_TO_PROVIDER["workbuddy_ai_login"], "workbuddy-ai"
        )
        self.assertEqual(
            workbuddy.PROVIDER_TO_AUTH_KIND["workbuddy-ai"], "workbuddy_ai_login"
        )

    def test_status_without_a_staged_package_reports_unavailable(self) -> None:
        root = tempfile.mkdtemp()
        runtime = workbuddy.WorkBuddyRuntime(root)
        with unittest.mock.patch.object(workbuddy, "available", return_value=False):
            status = runtime.status()
            self.assertFalse(status["available"])
            self.assertEqual(status["detail"], "integration_unstaged")
            models = runtime.models("workbuddy")
            self.assertFalse(models["available"])
            self.assertEqual(models["models"], [])

    def test_status_never_starts_a_worker_when_the_package_is_missing(self) -> None:
        root = tempfile.mkdtemp()
        runtime = workbuddy.WorkBuddyRuntime(root)
        with unittest.mock.patch.object(workbuddy, "available", return_value=False):
            self.assertFalse(runtime.ensure_started())
            self.assertEqual(runtime.environment(), {})

    def test_a_fresh_catalog_is_remembered_and_an_explicit_read_refreshes(self) -> None:
        """The 倍率 column does not pay for a worker round trip per model switch.

        Every route on one account shows a rate from that account's single
        catalog, and the pane reads it once per selected model.  Without a
        remembered answer each switch started the worker and asked the desktop
        app to resolve a credential and fetch the roster again.
        """

        root = tempfile.mkdtemp()
        runtime = workbuddy.WorkBuddyRuntime(root)
        document = {
            "available": True,
            "source": "live",
            "fetchedAtMs": 1,
            "models": [{"id": "glm-5.3", "billing": {"credits": "x0.79 credits"}}],
            "status": {"state": "signed-in"},
        }
        with unittest.mock.patch.object(workbuddy, "available", return_value=True), unittest.mock.patch.object(
            runtime, "_request", return_value=dict(document)
        ) as request:
            first = runtime.models("workbuddy")
            self.assertEqual([model["id"] for model in first["models"]], ["glm-5.3"])
            self.assertEqual(request.call_count, 1)
            self.assertNotIn("refresh=1", request.call_args.args[0])

            # A second plain read is answered from the remembered catalog.
            second = runtime.models("workbuddy")
            self.assertEqual(second["models"], first["models"])
            self.assertEqual(request.call_count, 1)

            # An explicit read reaches upstream again, and says so on the wire.
            runtime.models("workbuddy", refresh=True)
            self.assertEqual(request.call_count, 2)
            self.assertIn("refresh=1", request.call_args.args[0])

            # The answer is this object's own: a caller cannot mutate the cache.
            second["models"].append({"id": "invented"})
            self.assertEqual(len(runtime.models("workbuddy")["models"]), 1)

    def test_a_failed_catalog_read_is_never_remembered(self) -> None:
        """A signed-out account must not stay signed-out in the cache."""

        root = tempfile.mkdtemp()
        runtime = workbuddy.WorkBuddyRuntime(root)
        with unittest.mock.patch.object(workbuddy, "available", return_value=True):
            with unittest.mock.patch.object(
                runtime,
                "_request",
                return_value={"available": False, "status": {"state": "signed-out"}, "models": []},
            ) as request:
                self.assertFalse(runtime.models("workbuddy")["available"])
                self.assertFalse(runtime.models("workbuddy")["available"])
                self.assertEqual(request.call_count, 2)

            # An available-but-empty read is the same hazard: remembering it
            # would blank every 倍率 until the interval expired.
            with unittest.mock.patch.object(
                runtime,
                "_request",
                return_value={"available": True, "models": [], "status": {"state": "signed-in"}},
            ) as request:
                runtime.models("workbuddy")
                runtime.models("workbuddy")
                self.assertEqual(request.call_count, 2)


# The staged worker's own product knowledge is a Node process this Core owns,
# so a lifecycle test drives a real subprocess instead of a stub: the stand-in
# binds one loopback port, announces itself exactly as the worker does, and is
# killed the way a worker that the upstream sidecar ends dies.
_FAKE_WORKER = '''\
import json
import os
import signal
import socket
import sys
import threading
import time

arguments = sys.argv[1:]
options = dict(zip(arguments[0::2], arguments[1::2]))
port = int(options.get("--port") or 0)
root = options.get("--root") or "."
lifetime = float(os.environ.get("FAKE_WORKER_LIFETIME", "3600"))
crlf = bytes((13, 10))

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", port))
listener.listen(8)
bound = listener.getsockname()[1]
with open(os.path.join(root, "starts.log"), "a", encoding="utf-8") as handle:
    handle.write(f"{bound}\\n")


def serve():
    while True:
        connection, _ = listener.accept()
        try:
            request = connection.recv(65536)
            body = json.dumps({"ready": True, "request": request.splitlines()[:1]}).encode("utf-8")
            connection.sendall(crlf.join([
                b"HTTP/1.1 200 OK",
                b"Content-Type: application/json",
                b"Content-Length: " + str(len(body)).encode("ascii"),
                b"",
                body,
            ]))
        except OSError:
            pass
        finally:
            connection.close()


threading.Thread(target=serve, daemon=True).start()
print(json.dumps({"ready": True, "port": bound, "providers": ["workbuddy", "workbuddy-ai"]}), flush=True)
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
time.sleep(lifetime)
'''


class WorkBuddyWorkerLifecycleTests(unittest.TestCase):
    """A worker that dies on its own is Core's to replace, not the user's.

    The running proxy resolved its WorkBuddy base URL from its own environment
    when it launched, and nothing on the request path asks Core for a worker, so
    a worker that the upstream sidecar ends would otherwise leave every
    WorkBuddy route (DeepSeek's included) failing with a connection error for
    as long as the app runs - and a pane read would be the only cure.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.worker = self.root / "fake_worker.py"
        self.worker.write_text(_FAKE_WORKER, encoding="utf-8")
        self.saved_environment = {
            name: os.environ.get(name)
            for name in (*workbuddy.API_BASE_ENV.values(), *workbuddy.API_KEY_ENV.values())
        }
        self.addCleanup(self._restore_environment)
        for patcher in (
            mock.patch.object(workbuddy, "available", lambda: True),
            mock.patch.object(workbuddy, "staged_root", lambda: self.root / "staged"),
            mock.patch.object(workbuddy, "worker_path", lambda: self.worker),
            mock.patch.object(workbuddy, "node_command", lambda: sys.executable),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore_environment(self) -> None:
        for name, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _runtime(self) -> workbuddy.WorkBuddyRuntime:
        runtime = workbuddy.WorkBuddyRuntime(self.root)
        self.addCleanup(runtime.stop)
        return runtime

    def _starts(self) -> int:
        log = self.root / ".litellm-runtime" / "workbuddy" / "starts.log"
        if not log.is_file():
            return 0
        return len(log.read_text(encoding="utf-8").splitlines())

    def _wait_for(self, predicate) -> bool:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_a_worker_that_settled_before_it_died_is_replaced_at_once(self) -> None:
        settled = mock.patch.object(workbuddy, "_WORKBUDDY_SETTLED_SECONDS", 0.0)
        settled.start()
        self.addCleanup(settled.stop)
        runtime = self._runtime()
        self.assertTrue(runtime.ensure_started())
        first = runtime._process
        port = runtime._port
        self.assertIsNotNone(first)
        self.assertGreater(port, 0)

        first.kill()

        self.assertTrue(self._wait_for(lambda: runtime._process not in (None, first)))
        # The proxy child keeps calling the address it launched with, so the
        # replacement has to serve that one - not a fresh port nobody knows.
        self.assertEqual(runtime._port, port)
        self.assertEqual(
            runtime.environment_values()[workbuddy.API_BASE_ENV[workbuddy.WORKBUDDY_PROVIDER]],
            f"http://127.0.0.1:{port}/workbuddy/v1",
        )
        self.assertEqual(self._starts(), 2)
        replacement = runtime._process
        self.assertIsNotNone(replacement)
        self.assertIsNone(replacement.poll())

    def test_a_released_worker_is_not_resurrected_by_its_supervision(self) -> None:
        runtime = self._runtime()
        self.assertTrue(runtime.ensure_started())
        first = runtime._process
        self.assertIsNotNone(first)

        # Releasing the worker (every WorkBuddy route went away) must end its
        # supervision too: a credential-holding process is never left behind.
        runtime.stop()

        self.assertTrue(self._wait_for(lambda: first.poll() is not None))
        time.sleep(0.5)
        self.assertIsNone(runtime._process)
        self.assertEqual(runtime.environment(), {})
        self.assertEqual(self._starts(), 1)

    def test_a_failed_start_keeps_the_address_the_proxy_was_given(self) -> None:
        runtime = self._runtime()
        self.assertTrue(runtime.ensure_started())
        port = runtime._port
        self.assertGreater(port, 0)

        # A start that produces no worker - a Node it cannot reach, a worker
        # that never announces itself - must not send the next attempt to a
        # port nobody was told about.
        with mock.patch.object(runtime, "_spawn", lambda *_: None):
            self.assertFalse(runtime._start())

        self.assertEqual(runtime._port, port)
        self.assertEqual(
            runtime.environment_values()[workbuddy.API_BASE_ENV[workbuddy.WORKBUDDY_PROVIDER]],
            f"http://127.0.0.1:{port}/workbuddy/v1",
        )

    def test_one_failed_start_does_not_end_the_supervision(self) -> None:
        delays = (0.0, 0.05, 0.05)
        runtime = self._runtime()
        self.assertTrue(runtime.ensure_started())
        first = runtime._process
        port = runtime._port
        self.assertIsNotNone(first)
        spawn = runtime._spawn
        attempts = {"count": 0}

        def flaky(node: str, worker: Path, target: int):
            attempts["count"] += 1
            # Both attempts of one restart fail, the way a spawn can fail
            # under load; the next step has to try again on the same address.
            return None if attempts["count"] <= 2 else spawn(node, worker, target)

        with mock.patch.object(workbuddy, "_WORKBUDDY_RESTART_DELAYS_SECONDS", delays), mock.patch.object(
            runtime, "_spawn", flaky
        ):
            first.kill()
            self.assertTrue(self._wait_for(lambda: runtime._process not in (None, first)))

        self.assertGreater(attempts["count"], 2)
        self.assertEqual(runtime._port, port)
        replacement = runtime._process
        self.assertIsNotNone(replacement)
        self.assertIsNone(replacement.poll())

    def test_a_demand_read_refills_the_supervision_budget(self) -> None:
        delays = (0.0, 0.05, 0.05)
        for patcher in (
            mock.patch.dict(os.environ, {"FAKE_WORKER_LIFETIME": "0.05"}),
            mock.patch.object(workbuddy, "_WORKBUDDY_RESTART_DELAYS_SECONDS", delays),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        runtime = self._runtime()

        runtime.ensure_started()
        self.assertTrue(self._wait_for(lambda: runtime._restart_step >= len(delays)))
        spent = self._starts()
        time.sleep(0.3)
        self.assertEqual(self._starts(), spent)

        # A read the user's own pane made starts over with the whole budget,
        # so the worker it asks for is supervised too.
        self.assertTrue(runtime.ensure_started())
        self.assertTrue(self._wait_for(lambda: self._starts() > spent + 1))

    def test_a_worker_that_keeps_dying_young_does_not_spin(self) -> None:
        delays = (0.0, 0.05, 0.05)
        for patcher in (
            mock.patch.dict(os.environ, {"FAKE_WORKER_LIFETIME": "0.05"}),
            mock.patch.object(workbuddy, "_WORKBUDDY_RESTART_DELAYS_SECONDS", delays),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        runtime = self._runtime()

        runtime.ensure_started()
        # One demand start, then one supervised restart per delay step but the
        # last: the budget runs out on the final young death.
        expected = 1 + len(delays) - 1
        self.assertTrue(self._wait_for(lambda: self._starts() >= expected))
        settled = self._starts()
        self.assertLessEqual(settled, 1 + len(delays))
        # The steps run out and the demand path owns the next attempt: no
        # restart storm while the integration stays broken.
        time.sleep(0.5)
        self.assertEqual(self._starts(), settled)
        self.assertIsNone(runtime._process)


if __name__ == "__main__":
    unittest.main()
