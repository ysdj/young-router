"""WorkBuddy (CN / international) provider integration.

The upstream protocol is a third-party package staged at build time, so these
tests drive the seam instead of the network: a stub runtime stands in for the
worker and asserts what Core asks it for and what the provider domain does with
the answer.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock
import unittest.mock

from young_router import workbuddy
from young_router.core.domains import DomainError
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.provider_auth import ProviderAuthManager


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


class StubRuntime:
    """The worker surface Core depends on, without a Node process."""

    def __init__(self, *, state: str = "signed-in", available: bool = True) -> None:
        self.state = state
        self.available = available
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
            "models": [dict(entry) for entry in CATALOG],
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

    def test_the_login_type_is_an_ordinary_editable_field(self) -> None:
        """Switching the type rewires the address, the key slot and the routes."""

        directory, domain, _config = self._domain()
        with directory:
            added = domain.dispatch("service_provider.add", {"kind": "workbuddy_login"})
            provider_id = added["providers"][0]["id"]
            retyped = domain.dispatch(
                "service_provider.patch",
                {"provider_id": provider_id, "provider": {"auth_kind": "workbuddy_ai_login"}},
            )
            self.assertEqual(retyped["operation_summary"]["auth_kind"], "workbuddy_ai_login")
            provider = retyped["providers"][0]
            self.assertEqual(provider["api_base"], workbuddy.api_base_reference("workbuddy-ai"))
            self.assertEqual(provider["api_key_names"], [workbuddy.API_KEY_NAME])
            self.assertEqual(provider["extra"]["x-young-router-provider-auth"]["kind"], "workbuddy_ai_login")
            self.assertIs(provider["enabled"], False)
            for model in provider["models"]:
                self.assertEqual(model["api_base"], workbuddy.api_base_reference("workbuddy-ai"))
                self.assertEqual(model["api_key_name"], workbuddy.API_KEY_NAME)

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


class WorkBuddyModuleTests(unittest.TestCase):
    def test_core_resolves_the_references_it_owns(self) -> None:
        """A Core-side read sees the same loopback base and bearer as the child."""

        import os

        from young_router import workbuddy as workbuddy_module
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

        from young_router import workbuddy as workbuddy_module
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

        from young_router import workbuddy as workbuddy_module
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

    def test_a_remembered_worker_publishes_its_environment(self) -> None:
        """Core resolves the loopback base and bearer before it starts one."""

        import os

        from young_router import workbuddy as workbuddy_module

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


if __name__ == "__main__":
    unittest.main()
