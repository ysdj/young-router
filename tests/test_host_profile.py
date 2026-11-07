"""What a host serves, as distinct from what the shared document says.

One document is edited wherever you are and reaches the other machines over
WebDAV, so a few facts that are true of one machine have to be applied on that
machine rather than written into the document: a loopback provider is somebody
else's port, a proxy key other people's clients present is that host's own, and
a callback is code this install has to be able to import.

These tests pin the projection and the one seam that matters -- the staged
document, because that is what the proxy loads.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from young_router.core import host_profile


def _document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "providers": {
            "local": {"api_base": "http://127.0.0.1:41234/v1"},
            "worker": {"api_base": "os.environ/YOUNG_ROUTER_WORKBUDDY_BASE"},
            "remote": {"api_base": "https://api.example.test/v1"},
            "off": {"api_base": "https://off.example.test/v1", "enabled": False},
            "bare": {},
        },
        "model_list": [
            {
                "model_name": "remote-chat",
                "litellm_params": {"model": "openai/chat", "api_base": "https://api.example.test/v1"},
                "model_info": {"provider": "remote"},
            },
            {
                "model_name": "worker-lane",
                "litellm_params": {"model": "openai/lane", "api_base": "os.environ/YOUNG_ROUTER_WORKBUDDY_BASE"},
                "model_info": {"provider": "worker"},
            },
            {
                "model_name": "local-only",
                "litellm_params": {"model": "openai/local", "api_base": "http://127.0.0.1:41234/v1"},
                "model_info": {"provider": "local"},
            },
            {
                "model_name": "parked",
                "litellm_params": {"model": "openai/parked", "api_base": "https://api.example.test/v1"},
                "model_info": {"provider": "remote", "x-young-router-model-enabled": False},
            },
            {
                "model_name": "claude-alias",
                "litellm_params": {"model": "anthropic/claude-x", "api_base": "https://api.anthropic.test/v1"},
                "model_info": {"provider": "remote"},
            },
        ],
        "litellm_settings": {
            "public_model_groups": ["remote-chat", "worker-lane", "local-only", "parked", "claude-alias"],
            "callbacks": ["desktop.some.prompt_shim"],
        },
        "general_settings": {"master_key": "sk-published", "ui": True},
        "router_settings": {},
    }
    document.update(overrides)
    return document


class HostProfileProjectionTests(unittest.TestCase):
    def test_a_loopback_provider_is_somebody_elses_port(self) -> None:
        projected = host_profile.document(_document(), "sk-host")
        self.assertEqual({"worker", "remote"}, set(projected["providers"]))
        names = [entry["model_name"] for entry in projected["model_list"]]
        # The local route is gone; the ``os.environ/`` one travels, because the
        # launcher on this host resolves it from the worker it started here.
        self.assertEqual(["remote-chat", "worker-lane", "claude-alias"], names)

    def test_a_parked_row_is_not_served_even_though_it_is_in_the_document(self) -> None:
        projected = host_profile.document(_document(), "sk-host")
        self.assertNotIn("parked", [entry["model_name"] for entry in projected["model_list"]])
        self.assertNotIn("parked", projected["litellm_settings"]["public_model_groups"])

    def test_a_public_group_that_no_longer_routes_is_pruned(self) -> None:
        projected = host_profile.document(_document(), "sk-host")
        self.assertEqual(
            ["remote-chat", "worker-lane", "claude-alias"],
            projected["litellm_settings"]["public_model_groups"],
        )

    def test_the_anthropic_base_loses_its_version_suffix(self) -> None:
        """LiteLLM appends ``/v1/messages`` itself; ``/v1`` would become ``/v1/v1``."""

        projected = host_profile.document(_document(), "sk-host")
        claude = next(entry for entry in projected["model_list"] if entry["model_name"] == "claude-alias")
        self.assertEqual("https://api.anthropic.test", claude["litellm_params"]["api_base"])

    def test_the_callback_list_is_the_one_this_install_can_run(self) -> None:
        """The hook is the data plane; another machine's callback is not code here."""

        projected = host_profile.document(_document(), "sk-host")
        self.assertEqual([host_profile.HOOK_CALLBACK], projected["litellm_settings"]["callbacks"])

    def test_the_key_and_the_dashboard_are_the_hosts(self) -> None:
        projected = host_profile.document(_document(), "sk-host")
        self.assertEqual("sk-host", projected["general_settings"]["master_key"])
        self.assertIs(False, projected["general_settings"]["ui"])
        self.assertIs(False, projected["general_settings"]["dangerously_permit_weak_or_unset_master_key"])
        self.assertIs(True, projected["router_settings"]["disable_cooldowns"])

    def test_everything_else_is_passed_through_untouched(self) -> None:
        """Ordering is the user's measurement, not something a host re-decides."""

        document = _document(router_settings={"disable_cooldowns": False, "num_retries": 4})
        projected = host_profile.document(document, "sk-host")
        self.assertEqual({"disable_cooldowns": False, "num_retries": 4}, projected["router_settings"])

    def test_the_profile_is_off_unless_a_host_says_so(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(host_profile.active())
            self.assertEqual("", host_profile.stated_key())
            self.assertIsNone(host_profile.stated_port())
        with mock.patch.dict("os.environ", {host_profile.PROFILE_ENV: "gateway"}, clear=True):
            self.assertTrue(host_profile.active())

    def test_a_desktop_states_no_port_even_when_its_environment_names_one(self) -> None:
        """A desktop's port is the setting's; an inherited variable is not a statement."""

        with mock.patch.dict("os.environ", {"LITELLM_PORT": "4401"}, clear=True):
            self.assertIsNone(host_profile.stated_port())

    def test_a_host_states_its_port_under_either_name(self) -> None:
        """The unit names ``LV_PORT``; the launcher hands the proxy ``LITELLM_PORT``."""

        with mock.patch.dict(
            "os.environ", {host_profile.PROFILE_ENV: "gateway", "LITELLM_PORT": "4401"}, clear=True
        ):
            self.assertEqual(4401, host_profile.stated_port())
        with mock.patch.dict(
            "os.environ", {host_profile.PROFILE_ENV: "gateway", "LV_PORT": "4401"}, clear=True
        ):
            self.assertEqual(4401, host_profile.stated_port())
        # A range violation is not a statement either: the pane and the launch
        # path fall back to the setting instead of binding something imaginary.
        with mock.patch.dict(
            "os.environ", {host_profile.PROFILE_ENV: "gateway", "LITELLM_PORT": "70000"}, clear=True
        ):
            self.assertIsNone(host_profile.stated_port())

    def test_a_stated_key_is_read_from_the_file_the_unit_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "gateway-master-key"
            key_file.write_text("sk-lv-stated\n", encoding="utf-8")
            with mock.patch.dict(
                "os.environ", {host_profile.PROXY_KEY_ENV: str(key_file)}, clear=True
            ):
                self.assertEqual("sk-lv-stated", host_profile.stated_key())
            key_file.unlink()
            with mock.patch.dict(
                "os.environ", {host_profile.PROXY_KEY_ENV: str(key_file)}, clear=True
            ):
                self.assertEqual("", host_profile.stated_key())


class HostProfileStagingTests(unittest.TestCase):
    """The seam that matters: the document the proxy is handed."""

    CONFIG = """\
        providers:
          local:
            api_base: http://127.0.0.1:41234/v1
            api_keys:
              - name: default
                value: local-secret
          remote:
            api_base: https://api.example.test/v1
            api_keys:
              - name: default
                value: remote-secret
        model_list:
          - model_name: local-chat
            litellm_params:
              model: openai/local
              api_base: http://127.0.0.1:41234/v1
              api_key: local-secret
            model_info:
              id: aaaa0001
              provider: local
              upstream_url_surface: openai/responses
          - model_name: remote-chat
            litellm_params:
              model: openai/chat
              api_base: https://api.example.test/v1
              api_key: remote-secret
            model_info:
              id: aaaa0002
              provider: remote
              upstream_url_surface: openai/responses
        general_settings:
          master_key: sk-published
          ui: true
        litellm_settings:
          public_model_groups: [local-chat, remote-chat]
        """

    def test_staging_serves_the_projection_and_leaves_the_editor_document_alone(self) -> None:
        from young_router.core.operations import CoreServiceController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # The two documents a host has: the one the editor writes, and the
            # staged copy the proxy loads.  A desktop keeps them at one path;
            # a host states the editor's own path, which is what makes the
            # difference visible here.
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent(self.CONFIG).lstrip(), encoding="utf-8")
            key_file = root / "gateway-master-key"
            key_file.write_text("sk-lv-this-host\n", encoding="utf-8")

            with mock.patch.dict(
                "os.environ",
                {
                    "YOUNG_ROUTER_HOST_PROFILE": "gateway",
                    "YOUNG_ROUTER_HOST_PROXY_KEY_FILE": str(key_file),
                    "LITELLM_CONFIG_FILE": str(config_path),
                },
            ):
                controller = CoreServiceController(root)
                controller._stage_runtime_config()

            import yaml

            served = yaml.safe_load(controller.paths.runtime_config.read_text(encoding="utf-8"))
            self.assertEqual({"remote"}, set(served["providers"]))
            self.assertEqual(["remote-chat"], [entry["model_name"] for entry in served["model_list"]])
            self.assertEqual("sk-lv-this-host", served["general_settings"]["master_key"])
            self.assertEqual([host_profile.HOOK_CALLBACK], served["litellm_settings"]["callbacks"])

            # The document a person edits keeps everything, including the row
            # this host does not serve: it is shared, not this machine's.
            edited = config_path.read_text(encoding="utf-8")
            self.assertIn("local-chat", edited)
            self.assertIn("sk-published", edited)

    def test_a_desktop_stages_its_document_exactly_as_edited(self) -> None:
        from young_router.core.operations import CoreServiceController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent(self.CONFIG).lstrip(), encoding="utf-8")

            with mock.patch.dict("os.environ", {"LITELLM_CONFIG_FILE": str(config_path)}, clear=True):
                controller = CoreServiceController(root)
                controller._stage_runtime_config()

            import yaml

            served = yaml.safe_load(controller.paths.runtime_config.read_text(encoding="utf-8"))
            self.assertEqual({"local", "remote"}, set(served["providers"]))
            self.assertEqual("sk-published", served["general_settings"]["master_key"])


if __name__ == "__main__":
    unittest.main()
