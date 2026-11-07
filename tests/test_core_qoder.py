"""Qoder (CN / international) provider integration.

The upstream protocol is a third-party package staged at build time, so these
tests drive the seam instead of the network: a stub runtime stands in for the
worker and asserts what Core asks it for and what the provider domain does with
the answer.  The PAT never crosses the IPC payload; it is staged through the
secret capability and handed to the worker by target.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from young_router.adapters import qoder
from young_router.core.domains import DomainError
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.provider_auth import ProviderAuthManager


CATALOG = [
    {
        "id": "qwen3.8-max",
        "name": "Qwen3.8-Max",
        "publicName": "Qwen3.8-Max",
        "contextWindow": 200_000,
        "maxTokens": 32_000,
        "supportsImages": True,
        "reasoning": True,
        "rate": 0.5,
    },
    {
        "id": "qwen3.8-flash",
        "name": "Qwen3.8-Flash",
        "publicName": "Qwen3.8-Flash",
        "contextWindow": 200_000,
        "maxTokens": 32_000,
        "supportsImages": True,
        "rate": 0.0,
    },
]


class StubRuntime:
    """The worker surface Core depends on, without a Node process."""

    def __init__(self, *, state: str = "signed-in", available: bool = True) -> None:
        self.state = state
        self.available = available
        self.catalog = CATALOG
        self.calls: list[tuple[str, str, str]] = []
        self.stored: dict[str, str] = {}
        self.stopped = 0

    def provider_status(self, provider: str) -> dict[str, object]:
        self.calls.append(("provider_status", provider, ""))
        if not self.available:
            return {}
        return {
            "provider": provider,
            "state": self.state,
            "nickname": "Example User",
            "domain": "gateway.qoder.com.cn" if provider == "qoder" else "api1.qoder.sh",
            "catalogSource": "live",
            "modelCount": len(CATALOG),
            "credits": {"total": 92, "packages": []},
        }

    def status(self, *, refresh: bool = False) -> dict[str, object]:
        self.calls.append(("status", "refresh" if refresh else "read", ""))
        if not self.available:
            return {"available": False, "detail": "integration_unstaged", "providers": {}}
        return {
            "available": True,
            "providers": {name: self.provider_status(name) for name in qoder.QODER_PROVIDERS},
        }

    def models(self, provider: str, *, refresh: bool = False) -> dict[str, object]:
        self.calls.append(("models", provider, "refresh" if refresh else "read"))
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

    def open_desktop_app(self, provider: str) -> dict[str, object]:
        self.calls.append(("open_desktop_app", provider, ""))
        return {
            "provider": provider,
            "app_name": "Qoder CN",
            "app_path": "/Applications/Qoder CN.app",
            "installed": True,
            "opened": True,
        }

    def desktop_app(self, provider: str) -> dict[str, object]:
        self.calls.append(("desktop_app", provider, ""))
        return {
            "provider": provider,
            "app_name": "Qoder CN",
            "app_path": "/Applications/Qoder CN.app",
            "installed": True,
        }

    def login(self, provider: str, pat: object) -> dict[str, object]:
        value = str(pat or "")
        self.calls.append(("login", provider, value))
        if not value.startswith("pt-"):
            raise qoder.QoderUnavailable("personal token is invalid")
        self.stored[provider] = value
        self.state = "signed-in"
        return {"provider": provider, "available": True, "status": self.provider_status(provider)}

    def logout(self, provider: str) -> dict[str, object]:
        self.calls.append(("logout", provider, ""))
        self.stored.pop(provider, None)
        # The desktop app's own sign-in is not this app's to end: the fixture
        # answers the state that is left, which is what the pane reports.
        return {
            "provider": provider,
            "removed": True,
            "app_session": self.state == "signed-in",
            "status": self.provider_status(provider),
        }

    def environment(self, *, autostart: bool = False) -> dict[str, str]:
        return {
            qoder.API_BASE_ENV[qoder.QODER_PROVIDER]: "http://127.0.0.1:1/qoder/v1",
            qoder.API_KEY_ENV[qoder.QODER_PROVIDER]: "local-token",
            qoder.API_BASE_ENV[qoder.QODER_GLOBAL_PROVIDER]: "http://127.0.0.1:1/qoder-global/v1",
            qoder.API_KEY_ENV[qoder.QODER_GLOBAL_PROVIDER]: "local-token",
        }

    def environment_values(self) -> dict[str, str]:
        return self.environment()

    def stop(self) -> None:
        self.stopped += 1


class QoderProviderTests(unittest.TestCase):
    def _domain(self, runtime: StubRuntime | None = None):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        domain = ProvidersModelsDomain(
            config,
            auth_manager=ProviderAuthManager(root),
            qoder=runtime if runtime is not None else StubRuntime(),
        )
        return directory, domain, config

    def test_an_empty_model_list_creates_an_entry_without_models(self) -> None:
        """An empty list is a statement, and stays one.

        The wizard's model step now names the entries it picked; a caller that
        states an empty list (the editor's own path, or a surface that lets the
        user start empty) still gets an entry with no routes.  Omitting the
        field remains the different statement below.
        """

        directory, domain, _config = self._domain()
        with directory:
            snapshot = domain.dispatch(
                "service_provider.add", {"kind": "qoder_login", "models": []}
            )
            provider = snapshot["providers"][0]
            self.assertEqual(provider["models"], [])
            # The slot still exists and is managed: the pick needs it.
            self.assertEqual(provider["key_states"][0]["name"], qoder.API_KEY_NAME)
            self.assertTrue(provider["key_states"][0]["managed"])

    def test_a_wizard_pick_files_the_catalog_label_in_lower_case(self) -> None:
        """The model step lists labels, and 完成 names the entries it picked.

        A pick is a selector, not a row: the adapter owns the wire key it is
        called by and the label Qoder's own menu shows.  Core resolves the pick
        back to that entry and files the public name in lower case, so a client
        never has to type the label's capitalisation while the upstream keeps
        the key the wire uses.
        """

        runtime = StubRuntime()
        runtime.catalog = [
            {
                "id": "qmodel_38max",
                "name": "Qwen3.8-Max",
                "publicName": "Qwen3.8-Max",
                "contextWindow": 200_000,
                "maxTokens": 32_000,
                "rate": 0.5,
            }
        ]
        directory, domain, _config = self._domain(runtime)
        with directory:
            provider = domain.dispatch(
                "service_provider.add",
                {"kind": "qoder_login", "name": "Qoder", "models": ["qmodel_38max"]},
            )["providers"][0]
            self.assertEqual(
                [(model["name"], model["upstream_model"]) for model in provider["models"]],
                [("qwen3.8-max", "qmodel_38max")],
            )
            # A selector the catalog no longer lists is refused rather than
            # guessed: a wrong match would add a route the user did not pick.
            with self.assertRaisesRegex(DomainError, "no longer lists"):
                domain.dispatch(
                    "service_provider.add",
                    {
                        "kind": "qoder_login",
                        "name": "Qoder Two",
                        "models": ["qwen3.8-gone"],
                    },
                )

    def test_a_create_that_omits_the_list_still_imports_the_catalog(self) -> None:
        """Omission is a different statement from an empty list, and stays one.

        A caller with no picker to offer (a headless add) asks for the account's
        own roster by stating no list at all.  The wizard never does that; the
        two are kept apart so neither can be read as the other.
        """

        directory, domain, _config = self._domain()
        with directory:
            snapshot = domain.dispatch("service_provider.add", {"kind": "qoder_login"})
            provider = snapshot["providers"][0]
            self.assertEqual(provider["auth_kind"], "qoder_login")
            self.assertEqual(provider["api_base"], qoder.api_base_reference("qoder"))
            self.assertEqual(
                provider["key_states"][0]["name"], qoder.API_KEY_NAME
            )
            self.assertTrue(provider["key_states"][0]["managed"])
            # The public name is the catalog's own label, filed in the spelling
            # an import states; the upstream keeps the key it is called by.  A
            # pane that showed the key advertised a slug no model menu contains,
            # and the label's capitalisation is not a name a client must match.
            self.assertEqual(
                [(model["name"], model["upstream_model"]) for model in provider["models"]],
                [("qwen3.8-max", "qwen3.8-max"), ("qwen3.8-flash", "qwen3.8-flash")],
            )
            # The one managed slot is restated on every imported route, which
            # is what makes the dumper emit a key for it.
            self.assertTrue(
                all(model["provider_key_id"] for model in provider["models"])
            )
            self.assertTrue(
                all(model["upstream_url_surface"] == "openai/chat"
                    for model in provider["models"])
            )

    def test_a_pat_staged_for_one_region_cannot_sign_in_the_other(self) -> None:
        runtime = StubRuntime(state="signed-out")
        directory, domain, _config = self._domain(runtime)
        with directory:
            domain.stage_secret("qoder_pat", "qoder", "pt-example-token")
            with self.assertRaisesRegex(DomainError, "target does not match"):
                domain.dispatch(
                    "qoder_login",
                    {"provider": "qoder-global", "secret_target": "qoder"},
                )
            self.assertEqual(runtime.stored, {})

    def test_the_global_region_gets_its_own_references(self) -> None:
        """The two regions are two providers, never two variants of one."""

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "qoder_global_login"}
            )["providers"][0]
            self.assertEqual(provider["auth_kind"], "qoder_global_login")
            self.assertEqual(
                provider["api_base"],
                qoder.api_base_reference("qoder-global"),
            )
            self.assertEqual(
                provider["key_states"][0]["name"], qoder.API_KEY_NAME
            )
            self.assertEqual(
                provider["models"][0]["upstream_url_surface"], "openai/chat"
            )

    def test_a_signed_out_region_refuses_to_create_a_provider(self) -> None:
        runtime = StubRuntime(state="signed-out")
        directory, domain, _config = self._domain(runtime)
        with directory:
            with self.assertRaisesRegex(DomainError, "Sign in to Qoder"):
                domain.dispatch("service_provider.add", {"kind": "qoder_login"})

    def test_a_pat_is_staged_by_target_and_never_crosses_the_action(self) -> None:
        runtime = StubRuntime(state="signed-out")
        directory, domain, _config = self._domain(runtime)
        with directory:
            domain.stage_secret("qoder_pat", "qoder", "pt-example-token")
            self.assertTrue(domain.secret_present("qoder_pat", "qoder"))
            snapshot = domain.dispatch(
                "qoder_login",
                {"provider": "qoder", "secret_target": "qoder"},
            )
            summary = snapshot["operation_summary"]
            self.assertEqual(summary["status"]["state"], "signed-in")
            self.assertEqual(runtime.stored, {"qoder": "pt-example-token"})
            # The staged copy is spent: the worker holds the only copy.
            self.assertFalse(domain.secret_present("qoder_pat", "qoder"))

    def test_a_refused_pat_stays_staged_for_a_corrected_retry(self) -> None:
        runtime = StubRuntime(state="signed-out")
        directory, domain, _config = self._domain(runtime)
        with directory:
            domain.stage_secret("qoder_pat", "qoder", "not-a-pat")
            with self.assertRaisesRegex(DomainError, "personal token is invalid"):
                domain.dispatch(
                    "qoder_login",
                    {"provider": "qoder", "secret_target": "qoder"},
                )
            # The native field stages once per typed value; a second press on
            # sign-in is not a second staging.  Dropping the value here made
            # that retry answer "PAT is required" for a secret still on screen.
            self.assertTrue(domain.secret_present("qoder_pat", "qoder"))
            domain.stage_secret("qoder_pat", "qoder", "pt-example-token")
            snapshot = domain.dispatch(
                "qoder_login",
                {"provider": "qoder", "secret_target": "qoder"},
            )
            self.assertEqual(snapshot["operation_summary"]["status"]["state"], "signed-in")
            self.assertFalse(domain.secret_present("qoder_pat", "qoder"))

    def test_a_signed_in_region_creates_its_provider_after_login(self) -> None:
        runtime = StubRuntime(state="signed-out")
        directory, domain, _config = self._domain(runtime)
        with directory:
            domain.stage_secret("qoder_pat", "qoder", "pt-example-token")
            domain.dispatch("qoder_login", {"provider": "qoder", "secret_target": "qoder"})
            snapshot = domain.dispatch("service_provider.add", {"kind": "qoder_login"})
            provider = snapshot["providers"][0]
            self.assertEqual(provider["auth_kind"], "qoder_login")
            self.assertEqual(provider["auth_status"], "signed_in")
            self.assertTrue(provider["auth_configured"])
            self.assertEqual(
                provider["auth_observed"]["nickname"], "Example User"
            )

    def test_following_the_rate_learns_it_from_the_qoder_catalog(self) -> None:
        """A Qoder route told to follow its rate reads that account's worker.

        The rate arrives when the edit needs it, not only when a pane read the
        catalog first: the edit is the moment the number is written, and the
        account's own worker is the only place it is stated.
        """

        runtime = StubRuntime()
        directory, domain, config = self._domain(runtime)
        with directory:
            provider = domain.dispatch("service_provider.add", {"kind": "qoder_login"})["providers"][0]
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
            self.assertEqual(followed["order_mode"], "relay_multiplier")
            self.assertEqual(followed["effective_order"], 0.5)
            self.assertEqual(domain._published_rates["qoder"]["qwen3.8-max"], 0.5)
            self.assertIn("models", [call[0] for call in runtime.calls])
            self.assertTrue(domain.validate()["valid"])
            self.assertTrue(domain.apply()["applied"])
            self.assertIn("order: 0.5", config.read_text(encoding="utf-8"))

    def test_a_catalog_read_records_the_rate_and_the_window(self) -> None:
        directory, domain, _config = self._domain()
        with directory:
            domain.dispatch("service_provider.add", {"kind": "qoder_login"})
            snapshot = domain.dispatch(
                "qoder_models", {"provider": "qoder", "refresh": True}
            )
            summary = snapshot["operation_summary"]
            self.assertEqual(summary["operation"], "qoder_models")
            self.assertEqual(summary["display_name"], "Qoder")
            self.assertEqual(domain._published_rates["qoder"]["qwen3.8-max"], 0.5)
            self.assertEqual(
                domain._catalog_windows["qoder"]["qwen3.8-max"], 200_000
            )

    def test_the_status_read_is_remembered_for_a_projection(self) -> None:
        directory, domain, _config = self._domain()
        with directory:
            snapshot = domain.dispatch("qoder_status", {"provider": "qoder", "refresh": True})
            self.assertTrue(snapshot["operation_summary"]["available"])
            observed = domain._managed_accounts_observed["qoder"]
            self.assertEqual(observed["state"], "signed-in")
            self.assertEqual(observed["credits"]["total"], 92)
            # The pane states where the credential comes from, so the read that
            # answered it has to keep that fact, not only the account.
            self.assertIn("signInSource", observed)
            # The name matters: a ``*credential*`` key is rewritten to
            # ``configured`` by the snapshot's redaction, so a source stated
            # under that name reaches the pane unreadable.
            self.assertNotIn("credentialSource", observed)
            # A projection reads the observation instead of the worker.
            provider = {
                "name": "Qoder",
                "auth_kind": "qoder_login",
                "auth_credential_ref": "provider-auth-example",
            }
            self.assertEqual(
                domain._provider_auth_status(provider, domain._provider_auth_state(provider)),
                "signed_in",
            )

    def test_a_catalog_read_does_not_blank_the_observed_credit(self) -> None:
        """A status without a credit is "not stated", not a zero balance."""

        directory, domain, _config = self._domain()
        with directory:
            domain._managed_account_observe(
                {"kind": "qoder_login"},
                {"state": "signed-in", "nickname": "Example User", "credits": {"total": 92}},
            )
            # A catalog read rides the account question, but its status carries
            # no credit document.  The observed balance must survive it.
            domain._managed_account_observe(
                {"kind": "qoder_login"},
                {"state": "signed-in", "nickname": "Example User"},
            )
            self.assertEqual(
                domain._managed_accounts_observed["qoder"]["credits"]["total"], 92
            )
            # A failed usage read reports the absence without a total.  That is
            # not a zero balance either.
            domain._managed_account_observe(
                {"kind": "qoder_login"},
                {"state": "signed-in", "credits": {"unavailable": True}},
            )
            self.assertEqual(
                domain._managed_accounts_observed["qoder"]["credits"]["total"], 92
            )

    def test_a_region_the_app_is_signed_into_needs_no_pat(self) -> None:
        """The desktop app's own session is the default account.

        Nothing is pasted: the account read reports the app's sign-in, the
        create imports its roster, and the pane is told where the credential
        came from — the three facts that make "add Qoder" a two-press action.
        """

        runtime = StubRuntime()
        runtime.provider_status = lambda provider: {
            **StubRuntime().provider_status(provider),
            "signInSource": "app",
        }
        directory, domain, _config = self._domain(runtime)
        with directory:
            status = domain.dispatch("qoder_status", {"provider": "qoder", "refresh": True})
            account = status["operation_summary"]["providers"]["qoder"]
            self.assertEqual(account["signInSource"], "app")
            snapshot = domain.dispatch("service_provider.add", {"kind": "qoder_login"})
            provider = snapshot["providers"][0]
            self.assertEqual(provider["auth_status"], "signed_in")
            self.assertTrue(provider["models"])
            # Nothing was staged as a PAT: the app's session is not a secret of
            # this app's, so the wizard's slot stays empty.
            self.assertFalse(domain.secret_present("qoder_pat", "qoder"))

    def test_forgetting_a_pat_says_the_app_still_holds_the_account(self) -> None:
        """A sign-out this app cannot perform is not reported as one."""

        directory, domain, _config = self._domain()
        with directory:
            summary = domain.dispatch(
                "qoder_logout", {"provider": "qoder"}
            )["operation_summary"]
            self.assertTrue(summary["removed"])
            self.assertTrue(summary["app_session"])

    def test_a_signed_out_region_opens_the_app_that_owns_the_sign_in(self) -> None:
        runtime = StubRuntime(state="signed-out")
        directory, domain, _config = self._domain(runtime)
        with directory:
            summary = domain.dispatch(
                "qoder_open_app", {"provider": "qoder"}
            )["operation_summary"]
            self.assertEqual(summary["operation"], "qoder_open_app")
            self.assertEqual(summary["app_name"], "Qoder CN")
            self.assertTrue(summary["opened"])
            self.assertIn(("open_desktop_app", "qoder", ""), runtime.calls)

    def test_a_pick_is_resolved_by_its_display_label(self) -> None:
        """The chooser shows names, so the pick arrives as one.

        A label resolves only when exactly one entry states it; the ``Name (id)``
        form the pane builds for a collision resolves to that id.  Everything
        else is refused rather than guessed, because a wrong match would add a
        route for a model the user did not pick.
        """

        entries = {
            "qwen3.8-max": {"id": "qwen3.8-max", "publicName": "Qwen3.8-Max"},
            "qwen3.8-flash": {"id": "qwen3.8-flash", "publicName": "Qwen3.8-Flash"},
            "twin-a": {"id": "twin-a", "publicName": "Twin"},
            "twin-b": {"id": "twin-b", "publicName": "Twin"},
        }
        resolve = ProvidersModelsDomain._catalog_entry_for_selector
        self.assertEqual(resolve(entries, "qwen3.8-max")["id"], "qwen3.8-max")
        self.assertEqual(resolve(entries, "Qwen3.8-Flash")["id"], "qwen3.8-flash")
        # A label two entries state is not a pick: the pane suffixes it with the
        # id, and that form names exactly one of them.
        self.assertIsNone(resolve(entries, "Twin"))
        self.assertEqual(resolve(entries, "Twin (twin-b)")["id"], "twin-b")
        self.assertIsNone(resolve(entries, "Twin (twin-c)"))
        self.assertIsNone(resolve(entries, "Qwen3.8-Max (twin-a)"))

        directory, domain, _config = self._domain()
        with directory:
            # An entry created without its roster, so the import has work to do.
            provider = domain.dispatch(
                "service_provider.add", {"kind": "qoder_login", "models": []}
            )["providers"][0]
            self.assertEqual(provider["models"], [])
            summary = domain.dispatch(
                "model.import_service_catalog",
                {"provider_id": provider["id"], "models": ["Qwen3.8-Max"]},
            )["operation_summary"]
            self.assertEqual(summary["imported"], 1)
            rows = domain.snapshot()["providers"][0]["models"]
            self.assertEqual(rows[0]["model_name"], "qwen3.8-max")
            self.assertEqual(rows[0]["upstream_model"], "qwen3.8-max")
            # The same model picked again by its id adds nothing: the entry is
            # already there, whichever spelling named it.
            again = domain.dispatch(
                "model.import_service_catalog",
                {"provider_id": provider["id"], "models": ["qwen3.8-max"]},
            )["operation_summary"]
            self.assertEqual(again["imported"], 0)

    def test_a_pick_keeps_the_catalog_spelling_when_the_user_kept_it(self) -> None:
        """The case question is asked once per pick, and Core files its answer.

        A catalog label with upper case in it is a choice: the pane asks whether
        to import it in lower case or exactly as the catalog states it, and the
        whole pick is filed under that one answer.  The label still names the
        entry the pick resolved to — the chooser showed it — and the upstream
        stays the id the wire is called by.
        """

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "qoder_login", "models": []}
            )["providers"][0]
            summary = domain.dispatch(
                "model.import_service_catalog",
                {
                    "provider_id": provider["id"],
                    "models": ["Qwen3.8-Max", "Qwen3.8-Flash"],
                    "public_name_case": "original",
                },
            )["operation_summary"]
            self.assertEqual(summary["imported"], 2)
            rows = domain.snapshot()["providers"][0]["models"]
            self.assertEqual(
                [("Qwen3.8-Max", "qwen3.8-max"), ("Qwen3.8-Flash", "qwen3.8-flash")],
                [(row["model_name"], row["upstream_model"]) for row in rows],
            )
            # Picked again without an answer, the same rows are found: the
            # spelling the user kept is not a second model.
            again = domain.dispatch(
                "model.import_service_catalog",
                {"provider_id": provider["id"], "models": ["Qwen3.8-Max"]},
            )["operation_summary"]
            self.assertEqual(again["imported"], 0)

    def test_the_registry_publishes_the_qoder_references(self) -> None:
        from young_router import adapters

        references = adapters.managed_base_references()
        self.assertIn("os.environ/YOUNG_ROUTER_QODER_BASE", references)
        self.assertIn("os.environ/YOUNG_ROUTER_QODER_GLOBAL_BASE", references)
        self.assertIn("qoder_login", adapters.catalog_auth_kinds())
        self.assertEqual(adapters.catalog_provider_id("qoder_login"), "qoder")


if __name__ == "__main__":
    unittest.main()
