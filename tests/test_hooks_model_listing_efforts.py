from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

from hook_test_utils import HookTestCase, load_hook_module


def _provider_config(*routes: tuple[str, str]) -> str:
    """Render a minimal ``model_list`` from ``(public name, deployment)``."""

    lines = ["model_list:"]
    for public_name, deployment in routes:
        lines.extend(
            [
                f"  - model_name: {public_name}",
                "    litellm_params:",
                f"      model: {deployment}",
            ]
        )
    return "\n".join(lines) + "\n"


class ModelListingEffortsTests(HookTestCase):
    """The listing a Pi client discovers its thinking levels from.

    Pi reads ``supported_efforts`` off each ``/v1/models`` entry and records
    every level the array omits as an explicit ``null``.  A route that only
    reports ``reasoning: true`` therefore loses ``xhigh``/``max`` for the
    client even though the proxy accepts and maps both, which is the whole
    reason this key exists.
    """

    @staticmethod
    def _record(thinking_level_map: dict[str, str | None]) -> dict:
        return {
            "context_window": 1000,
            "max_context_window": 1000,
            "source": "https://pi.dev/api/models",
            "priority": 40,
            "reasoning": True,
            "thinking_level_map": thinking_level_map,
        }

    def _temp_dir(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def _runtime(self, records: dict, *routes: tuple[str, str]) -> Path:
        root = self._temp_dir()
        (root / "model-contexts.json").write_text(
            json.dumps({"records": records}),
            encoding="utf-8",
        )
        config = root / "config.yaml"
        config.write_text(_provider_config(*routes), encoding="utf-8")
        self.set_env("CODEX_HOME", str(root / "codex"))
        self.set_env("LITELLM_RUNTIME_ROOT", str(root))
        self.set_env("LITELLM_CONFIG_FILE", str(config))
        self.set_env("YOUNG_ROUTER_RUNTIME_SETTINGS_FILE", str(root / "runtime-settings.env"))
        return root

    def _install_listing_patch(self, hooks, responses: dict, seen: dict | None = None):
        """Patch a stand-in ``litellm.proxy.utils`` and return its builder."""

        def create_model_info_response(model_id, provider="openai", **kwargs):
            if seen is not None:
                seen.setdefault("model_ids", []).append(model_id)
            return {
                "id": model_id,
                "object": "model",
                "created": 1677610602,
                "owned_by": provider,
            }

        # The real package is replaced for this process by the hook stub, so
        # the patch installs against a module the test owns.  Restore the real
        # entries afterwards: the stub delegates unknown names to the installed
        # litellm, and leaving a fake ``litellm.proxy.utils`` behind would
        # break a later test that reads it.
        previous_proxy = sys.modules.get("litellm.proxy")
        previous_utils = sys.modules.get("litellm.proxy.utils")
        proxy_module = types.ModuleType("litellm.proxy")
        utils_module = types.ModuleType("litellm.proxy.utils")
        utils_module.create_model_info_response = create_model_info_response
        proxy_module.utils = utils_module
        sys.modules["litellm.proxy"] = proxy_module
        sys.modules["litellm.proxy.utils"] = utils_module

        def restore() -> None:
            for name, module in (
                ("litellm.proxy", previous_proxy),
                ("litellm.proxy.utils", previous_utils),
            ):
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.addCleanup(restore)
        hooks._install_model_listing_efforts_patch()
        responses.update(
            {"builder": utils_module.create_model_info_response, "module": utils_module}
        )
        return utils_module.create_model_info_response

    def test_route_levels_are_published_and_an_image_route_adds_nothing(self) -> None:
        hooks, _ = load_hook_module()
        self._runtime(
            {
                "anthropic/claude-opus-5-5": self._record(
                    {
                        "off": None,
                        "minimal": None,
                        "low": "low",
                        "medium": "medium",
                        "high": "high",
                        "xhigh": "xhigh",
                        "max": "max",
                    }
                )
            },
            ("claude-opus-5-5", "anthropic/claude-opus-5-5"),
            ("gpt-image-2.5", "openai/gpt-image-2.5"),
        )
        response: dict = {}
        builder = self._install_listing_patch(hooks, response)

        listed = builder("claude-opus-5-5")
        self.assertEqual(
            ["low", "medium", "high", "xhigh", "max"],
            listed["supported_efforts"],
        )
        # The vendor shape is otherwise untouched, so a client that does not
        # know the key sees exactly what LiteLLM returned before.
        self.assertEqual("claude-opus-5-5", listed["id"])
        self.assertEqual("openai", listed["owned_by"])
        self.assertEqual(1677610602, listed["created"])

        # A model with no reasoning metadata keeps the vendor shape: an empty
        # array would read to Pi as "off only", which is a different claim
        # from "this route published nothing".
        self.assertNotIn("supported_efforts", builder("gpt-image-2.5"))

    def test_only_levels_every_deployment_of_the_route_agrees_on_are_published(self) -> None:
        """The published surface must not exceed what the request path maps.

        ``reasoning_for`` intersects its deployments' capabilities, and the
        request path clamps against the same resolution.  A route whose two
        deployments disagree therefore publishes the intersection rather than
        the more generous side.
        """

        hooks, _ = load_hook_module()
        self._runtime(
            {
                "openai/public-a-model": self._record(
                    {
                        "off": None,
                        "minimal": None,
                        "low": "low",
                        "medium": "medium",
                        "high": "high",
                        "xhigh": "xhigh",
                        "max": "max",
                    }
                ),
                "openai/public-b-model": self._record(
                    {
                        "off": None,
                        "minimal": None,
                        "low": "low",
                        "medium": "medium",
                        "high": "high",
                        "xhigh": None,
                        "max": None,
                    }
                ),
            },
            ("shared-model", "openai/public-a-model"),
            ("shared-model", "openai/public-b-model"),
        )
        builder = self._install_listing_patch(hooks, {})

        self.assertEqual(
            ["low", "medium", "high"],
            builder("shared-model")["supported_efforts"],
        )

    def test_a_relay_alias_without_a_provider_qualified_record_still_resolves(self) -> None:
        """A public alias is the route's name, not its registry key.

        The registry carries the provider-qualified spelling while the route
        the client calls is the bare alias, so a listing lookup that stopped
        at ``reasoning_for`` would silently publish nothing for every relay
        model -- the exact population this feature exists for.
        """

        hooks, _ = load_hook_module()
        self._runtime(
            {
                "opencode-go/space-bunny": self._record(
                    {
                        "off": None,
                        "minimal": None,
                        "low": "low",
                        "medium": "medium",
                        "high": "high",
                        "xhigh": "xhigh",
                        "max": None,
                    }
                )
            },
            ("space-bunny", "openai/space-bunny"),
        )
        builder = self._install_listing_patch(hooks, {})

        self.assertEqual(
            ["low", "medium", "high", "xhigh"],
            builder("space-bunny")["supported_efforts"],
        )

    def test_a_second_install_replaces_nothing_and_the_lookup_precedes_the_key(self) -> None:
        """Installation is idempotent and never masks the underlying builder."""

        hooks, _ = load_hook_module()
        self._runtime(
            {
                "anthropic/claude-opus-5-5": self._record({"high": "high"}),
            },
            ("claude-opus-5-5", "anthropic/claude-opus-5-5"),
        )
        seen: dict = {}
        response: dict = {}
        builder = self._install_listing_patch(hooks, response, seen)
        installed = response["module"].create_model_info_response

        # A second install must leave the already-wrapped builder in place:
        # wrapping it again would add a second resolution pass per listing row.
        hooks._install_model_listing_efforts_patch()
        self.assertIs(installed, response["module"].create_model_info_response)

        builder("claude-opus-5-5")
        # The wrapped builder still delegates to the original, so the vendor
        # fields the endpoint relies on are produced exactly once.
        self.assertEqual(["claude-opus-5-5"], seen["model_ids"])

    def test_an_unreadable_route_configuration_leaves_the_listing_shape_alone(self) -> None:
        """Discovery degrades to the vendor response, never to a failed listing."""

        hooks, _ = load_hook_module()
        root = self._temp_dir()
        # A configuration that cannot be parsed stands in for every failure of
        # the route index: a half-written file, an unreadable cache, a route
        # whose metadata source is unreachable.
        (root / "config.yaml").write_text("model_list: [\n", encoding="utf-8")
        (root / "model-contexts.json").write_text("{ not json", encoding="utf-8")
        self.set_env("CODEX_HOME", str(root / "codex"))
        self.set_env("LITELLM_RUNTIME_ROOT", str(root))
        self.set_env("LITELLM_CONFIG_FILE", str(root / "config.yaml"))
        self.set_env("YOUNG_ROUTER_RUNTIME_SETTINGS_FILE", str(root / "runtime-settings.env"))

        builder = self._install_listing_patch(hooks, {})

        self.assertNotIn("supported_efforts", builder("claude-opus-5-5"))


if __name__ == "__main__":
    import unittest

    unittest.main()
