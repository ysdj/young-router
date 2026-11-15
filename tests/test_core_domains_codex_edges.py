from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import tomllib
import unittest
from unittest import mock

from young_router.core.domains import _shared
from young_router.core.domains.codex import CodexSettingsDomain, _remove_legacy_computer_mcp


PROVIDER_CONFIG = """
providers:
  primary:
    api_base: "https://example.test/v1"
    api_keys:
      - name: default
        value: "replace-me-secret"
model_list:
  - model_name: default-chat
    litellm_params:
      model: openai/default-chat
      api_base: "https://example.test/v1"
      api_key: "replace-me-secret"
    model_info:
      id: "00000071"
      provider: primary
      upstream_url_surface: openai/responses
litellm_settings:
  public_model_groups: [default-chat]
general_settings:
  master_key: sk-synthetic-master
"""


class CodexSettingsDomainEdgeTests(unittest.TestCase):

    def test_following_an_openai_client_stops_after_the_first_rewrite(self) -> None:
        """A built-in OpenAI client converges: no provider row can carry the flag."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            first = "http://127.0.0.1:21999/v1"
            second = "http://127.0.0.1:22000/v1"
            (home / "config.toml").write_text(
                'model = "default-chat"\n'
                'model_provider = "openai"\n'
                f'openai_base_url = "{first}"\n',
                encoding="utf-8",
            )
            (home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-first"}\n', encoding="utf-8")
            with mock.patch.object(_shared, "local_proxy_endpoint", return_value=(first, "sk-first")):
                domain = CodexSettingsDomain(runtime, codex_home=home)
            with mock.patch.object(_shared, "local_proxy_endpoint", return_value=(second, "sk-second")):
                self.assertTrue(domain.follow_local_api_endpoint())
                self.assertFalse(domain.follow_local_api_endpoint())
            self.assertIn(second, domain.export(include_sensitive=True)["config_text"])

    def test_following_the_proxy_leaves_an_unapplied_draft_off_disk(self) -> None:
        """A staged provider edit is not published by the restart pass."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            first = "http://127.0.0.1:21999/v1"
            disk = (
                'model = "default-chat"\nmodel_provider = "relay"\n'
                f'[model_providers.relay]\nbase_url = "{first}"\n'
                '[model_providers.other]\nbase_url = "https://other.example.test/v1"\n'
            )
            (home / "config.toml").write_text(disk, encoding="utf-8")
            (home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-first"}\n', encoding="utf-8")
            with mock.patch.object(_shared, "local_proxy_endpoint", return_value=(first, "sk-first")):
                domain = CodexSettingsDomain(runtime, codex_home=home)
            domain.dispatch("patch", {"providers": [{"id": "other", "base_url": "https://staged.example.test/v1"}]})
            with mock.patch.object(_shared, "local_proxy_endpoint", return_value=("http://127.0.0.1:22000/v1", "sk-second")):
                self.assertFalse(domain.follow_local_api_endpoint())
            self.assertNotIn("staged.example.test", (home / "config.toml").read_text(encoding="utf-8"))

    def test_removing_the_legacy_computer_mcp_drops_its_nested_tables(self) -> None:
        """A server's nested tables go with the header; other servers stay."""

        text = (
            "[mcp_servers.computer-use]\n"
            'command = "old-client"\n'
            "\n"
            "[mcp_servers.computer-use.env]\n"
            'MODE = "legacy"\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "/usr/bin/other"\n'
        )
        result = _remove_legacy_computer_mcp(text)
        servers = tomllib.loads(result)["mcp_servers"]
        self.assertEqual({"other"}, set(servers))


if __name__ == "__main__":
    unittest.main()
