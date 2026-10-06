"""WorkBuddy (CN / international) provider integration.

The upstream protocol is a third-party package staged at build time, so these
tests drive the seam instead of the network: a stub runtime stands in for the
worker and asserts what Core asks it for and what the provider domain does with
the answer.
"""

from __future__ import annotations

import os
from pathlib import Path
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

        root = Path(__file__).resolve().parents[1]
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
