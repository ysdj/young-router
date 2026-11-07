from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

from young_router.core import codex_config


ROOT = Path(__file__).resolve().parents[1]


class CodexConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        # Several tests below substitute the transport; the listing memo is
        # process-wide so a previous test's answer must not answer this one.
        codex_config.clear_local_model_listing_cache()
        self.addCleanup(codex_config.clear_local_model_listing_cache)

    class _Response:
        def __init__(self, payload: object, status: int = 200):
            self.status = status
            self.body = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size: int = -1) -> bytes:
            return self.body if size < 0 else self.body[:size]

        def getcode(self) -> int:
            return self.status

    def write_runtime_config(self, path: Path) -> None:
        path.write_text(
            textwrap.dedent(
                """
                providers:
                  active:
                    api_base: https://active.example.test/v1
                    api_keys:
                      - name: default
                        value: replace-me
                  disabled:
                    enabled: false
                    api_base: https://disabled.example.test/v1
                    api_keys:
                      - name: default
                        value: replace-me-disabled
                model_list:
                  - model_name: active-chat
                    litellm_params:
                      model: openai/active-chat
                    model_info:
                      id: a1b2c3d4
                      provider: active
                      upstream_url_surface: openai/responses
                      supported_upstream_url_surfaces: [openai/responses]
                  - model_name: disabled-chat
                    litellm_params:
                      model: openai/disabled-chat
                    model_info:
                      id: a1b2c3d5
                      provider: active
                      x-young-router-model-enabled: false
                      upstream_url_surface: openai/responses
                      supported_upstream_url_surfaces: [openai/responses]
                  - model_name: unavailable-chat
                    litellm_params:
                      model: openai/unavailable-chat
                    model_info:
                      id: a1b2c3d6
                      provider: disabled
                      upstream_url_surface: openai/responses
                      supported_upstream_url_surfaces: [openai/responses]
                general_settings:
                  master_key: sk-test-local
                """
            ).lstrip(),
            encoding="utf-8",
        )

    def test_local_exposed_models_uses_authenticated_litellm_endpoint(self) -> None:
        opener = mock.Mock()
        opener.open.return_value = self._Response(
            {
                "data": [
                    {"id": "model-b"},
                    {"id": "Alpha-model"},
                    {"id": "model-a"},
                    {"id": "model-b"},
                    {"id": ""},
                    {"id": "invalid\nmodel"},
                ]
            }
        )

        with mock.patch.object(codex_config, "local_base_url", return_value="http://127.0.0.1:4999/v1"), mock.patch.object(
            codex_config.urllib.request,
            "build_opener",
            return_value=opener,
        ):
            models, enabled = codex_config.local_exposed_models("sk-local-test")

        self.assertTrue(enabled)
        # Every picker this app writes is read top-down and picked by number, so
        # the probe answers the one order they all share: alphabetical by public
        # name, case-insensitively.  It is settled here, in the only function the
        # Codex catalog and both Claude lists are built from, so no client re-sorts
        # its own copy into a different list.  ``/v1/models`` itself keeps
        # publishing in the order its ``model_list`` declares.
        self.assertEqual(["Alpha-model", "model-a", "model-b"], models)
        request = opener.open.call_args.args[0]
        self.assertEqual("http://127.0.0.1:4999/v1/models", request.full_url)
        self.assertEqual("Bearer sk-local-test", request.get_header("Authorization"))

    def test_local_exposed_model_rows_keeps_each_routes_context_hint(self) -> None:
        opener = mock.Mock()
        opener.open.return_value = self._Response(
            {
                "data": [
                    {"id": "one-million", "max_input_tokens": 1_048_576},
                    {"id": "unknown-window"},
                    {"id": "zero", "max_input_tokens": 0},
                    {"id": "flag", "max_input_tokens": True},
                    {"id": "image-only", "mode": "image_generation"},
                ]
            }
        )
        with mock.patch.object(
            codex_config, "local_base_url", return_value="http://127.0.0.1:4999/v1"
        ), mock.patch.object(
            codex_config.urllib.request, "build_opener", return_value=opener
        ), mock.patch.object(
            codex_config, "_public_catalog_is_image_model", return_value=False
        ):
            rows, enabled = codex_config.local_exposed_model_rows("sk-local-test")

        self.assertTrue(enabled)
        self.assertEqual(
            [
                {"id": "flag", "max_input_tokens": None},
                {"id": "one-million", "max_input_tokens": 1_048_576},
                {"id": "unknown-window", "max_input_tokens": None},
                {"id": "zero", "max_input_tokens": None},
            ],
            rows,
        )

    def test_one_observation_answers_every_reader_in_a_refresh(self) -> None:
        """The editor and both client lists share one ``/v1/models`` read.

        While the proxy replaces its workers the port is bound but silent, so
        each reader that asked again spent the endpoint timeout under the Core
        lock.  One short-lived observation per refresh is enough; a failure is
        remembered too, because it is the expensive answer.
        """

        opener = mock.Mock()
        opener.open.return_value = self._Response({"data": [{"id": "model-a"}]})
        with mock.patch.object(
            codex_config, "local_base_url", return_value="http://127.0.0.1:4999/v1"
        ), mock.patch.object(
            codex_config.urllib.request, "build_opener", return_value=opener
        ):
            first = codex_config.local_exposed_models("sk-cache-test")
            second = codex_config.local_exposed_models("sk-cache-test")

        self.assertEqual(first, second)
        self.assertEqual(1, opener.open.call_count)

        codex_config.clear_local_model_listing_cache()
        failing = mock.Mock()
        failing.open.side_effect = urllib.error.URLError("offline")
        with mock.patch.object(
            codex_config, "local_base_url", return_value="http://127.0.0.1:4999/v1"
        ), mock.patch.object(
            codex_config.urllib.request, "build_opener", return_value=failing
        ):
            first_miss = codex_config.local_exposed_models("sk-cache-miss")
            second_miss = codex_config.local_exposed_models("sk-cache-miss")

        self.assertEqual(([], False), first_miss)
        self.assertEqual(first_miss, second_miss)
        self.assertEqual(1, failing.open.call_count)

    def test_local_exposed_models_returns_none_when_menu_is_unavailable(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("offline")
        with mock.patch.object(codex_config.urllib.request, "build_opener", return_value=opener):
            models, enabled = codex_config.local_exposed_models("sk-local-test")

        self.assertFalse(enabled)
        self.assertEqual([], models)

    def test_local_exposed_models_drops_an_image_route_the_listing_states(self) -> None:
        """A generator is not a conversation, so no picker offers it.

        The listing keeps publishing the route -- ``/v1/models`` is untouched --
        and every picker this app writes skips it, because naming a generator as
        the model of a turn is a choice that can only fail.  The recorded turn
        for ``gpt-image-2.5-sunburst`` sent ``exec_command``/``apply_patch`` to a
        route that answers its own request body instead of a message.
        """

        opener = mock.Mock()
        opener.open.return_value = self._Response(
            {
                "data": [
                    {"id": "chat-model", "mode": "chat"},
                    {"id": "gpt-image-2.5-sunburst", "mode": "image_generation"},
                    {"id": "gpt-image-1", "mode": "image"},
                    # A route the public catalogs resolve and LiteLLM's own cost
                    # map does not still leaves the picker.
                    {"id": "vendor-image-xl"},
                    {"id": "no-mode-chat-model"},
                ]
            }
        )

        with mock.patch.object(
            codex_config, "local_base_url", return_value="http://127.0.0.1:4999/v1"
        ), mock.patch.object(
            codex_config.urllib.request, "build_opener", return_value=opener
        ), mock.patch.object(
            codex_config,
            "_public_catalog_is_image_model",
            side_effect=lambda name: name == "vendor-image-xl",
        ):
            models, enabled = codex_config.local_exposed_models("sk-local-test")

        self.assertTrue(enabled)
        self.assertEqual(["chat-model", "no-mode-chat-model"], models)

    def test_an_unreadable_public_catalog_does_not_hide_a_chat_route(self) -> None:
        """A registry that cannot be built is not evidence of an image model."""

        opener = mock.Mock()
        opener.open.return_value = self._Response(
            {"data": [{"id": "chat-model"}, {"id": "other-chat"}]}
        )

        with mock.patch.object(
            codex_config, "local_base_url", return_value="http://127.0.0.1:4999/v1"
        ), mock.patch.object(
            codex_config.urllib.request, "build_opener", return_value=opener
        ), mock.patch(
            "young_router.core.model_catalog.route_serves_only_images",
            side_effect=RuntimeError("cache unreadable"),
        ):
            models, enabled = codex_config.local_exposed_models("sk-local-test")

        self.assertTrue(enabled)
        self.assertEqual(["chat-model", "other-chat"], models)

    def test_litellm_model_selection_uses_local_checkpoint_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_config = Path(directory) / "config.yaml"
            self.write_runtime_config(runtime_config)
            with mock.patch.object(
                codex_config,
                "local_base_url",
                return_value="http://127.0.0.1:4999/v1",
            ):
                config_text, auth_text, _config, _auth = (
                    codex_config.apply_structured_patch(
                        'model = "previous"\n[features]\ngoals = true\n',
                        "{}\n",
                        {
                            "litellm_model": {
                                "model": "active-chat",
                                "provider": "active",
                                "deployment_id": "a1b2c3d4",
                            }
                        },
                        runtime_config,
                    )
                )

        parsed = tomllib.loads(config_text)
        self.assertEqual(codex_config.LITELLM_CODEX_PROVIDER_ID, parsed["model_provider"])
        self.assertEqual("active-chat", parsed["model"])
        self.assertTrue(parsed["features"]["goals"])
        self.assertNotIn("token_budget", parsed["features"])
        provider = parsed["model_providers"][codex_config.LITELLM_CODEX_PROVIDER_ID]
        self.assertEqual(codex_config.LITELLM_CODEX_PROVIDER_NAME, provider["name"])
        self.assertEqual("http://127.0.0.1:4999/v1", provider["base_url"])
        self.assertEqual("responses", provider["wire_api"])
        self.assertTrue(provider["requires_openai_auth"])
        self.assertEqual("sk-test-local", json.loads(auth_text)["OPENAI_API_KEY"])

    def test_litellm_model_selection_strips_legacy_forced_token_budget_once(self) -> None:
        selection = {
            "litellm_model": {
                "model": "active-chat",
                "provider": "active",
                "deployment_id": "a1b2c3d4",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime_config = Path(directory) / "config.yaml"
            self.write_runtime_config(runtime_config)
            with mock.patch.object(
                codex_config,
                "local_base_url",
                return_value="http://127.0.0.1:4999/v1",
            ):
                first, _auth_text, _config, _auth = codex_config.apply_structured_patch(
                    'model = "previous"\n[features]\ntoken_budget = true\ngoals = true\n',
                    "{}\n",
                    selection,
                    runtime_config,
                )

                marker = codex_config.legacy_token_budget_marker_path(runtime_config)
                self.assertTrue(marker.exists())
                parsed_first = tomllib.loads(first)
                self.assertNotIn("token_budget", parsed_first["features"])
                self.assertTrue(parsed_first["features"]["goals"])

                # After the one-time cleanup, an explicitly re-enabled value
                # survives later LiteLLM selections untouched.
                re_enabled = codex_config.set_table_value(
                    first, ("features",), "token_budget", True
                )
                second, _auth2, _config2, _auth3 = codex_config.apply_structured_patch(
                    re_enabled,
                    "{}\n",
                    selection,
                    runtime_config,
                )

                self.assertTrue(tomllib.loads(second)["features"]["token_budget"])

    def test_an_unset_search_opt_in_survives_a_provider_row_round_trip(self) -> None:
        """A missing ``supports_standalone_web_search`` must stay missing.

        The structured row is what the editor sends back on every provider edit.
        Reporting a missing opt-in as ``false`` made one unrelated edit write
        ``supports_standalone_web_search = false``, which retires the only web
        tool a Responses Lite route can have -- Codex then registers no
        ``web.run`` and the agent drives a browser instead of searching.  The
        reported value is therefore tri-state, and only an explicit ``false``
        may write ``false``.
        """

        with tempfile.TemporaryDirectory() as directory:
            runtime_config = Path(directory) / "config.yaml"
            self.write_runtime_config(runtime_config)
            config_text = textwrap.dedent(
                """
                model = "active-chat"
                model_provider = "newapi"

                [model_providers.newapi]
                name = "newapi"
                base_url = "http://127.0.0.1:12390/v1"
                wire_api = "responses"
                requires_openai_auth = true
                """
            ).lstrip()
            parsed = codex_config.parse_toml_text(config_text)
            reported = codex_config.structured_config(parsed, {})["providers"]
            self.assertIsNone(reported[0]["supports_standalone_web_search"])

            # The edit that used to erase the opt-in: anything about the row.
            untouched, _auth, _config, _parsed_auth = codex_config.apply_structured_patch(
                config_text, "{}\n", {"providers": reported}, runtime_config
            )
            self.assertNotIn(
                "supports_standalone_web_search",
                tomllib.loads(untouched)["model_providers"]["newapi"],
            )

            # Present stays present, and an explicit off still writes off.
            enabled_text = config_text + "supports_standalone_web_search = true\n"
            enabled_parsed = codex_config.parse_toml_text(enabled_text)
            enabled_rows = codex_config.structured_config(enabled_parsed, {})["providers"]
            self.assertIs(True, enabled_rows[0]["supports_standalone_web_search"])
            kept, _auth, _config, _parsed_auth = codex_config.apply_structured_patch(
                enabled_text, "{}\n", {"providers": enabled_rows}, runtime_config
            )
            self.assertIs(
                True,
                tomllib.loads(kept)["model_providers"]["newapi"][
                    "supports_standalone_web_search"
                ],
            )

            off_rows = [dict(enabled_rows[0], supports_standalone_web_search=False)]
            switched_off, _auth, _config, _parsed_auth = codex_config.apply_structured_patch(
                enabled_text, "{}\n", {"providers": off_rows}, runtime_config
            )
            self.assertIs(
                False,
                tomllib.loads(switched_off)["model_providers"]["newapi"][
                    "supports_standalone_web_search"
                ],
            )

    def test_litellm_model_selection_uses_non_openai_provider_for_local_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_config = Path(directory) / "config.yaml"
            self.write_runtime_config(runtime_config)
            config_text = textwrap.dedent(
                """
                model = "previous"
                model_provider = "newapi"
                openai_base_url = "https://stale-openai.example.test/v1"

                [model_providers.newapi]
                name = "OpenAI"
                base_url = "https://remote.example.test/v1"
                base_url1 = "https://fallback.example.test/v1"
                wire_api = "responses"
                requires_openai_auth = true
                supports_standalone_web_search = true
                """
            ).lstrip()

            with mock.patch.object(
                codex_config,
                "local_base_url",
                return_value="http://127.0.0.1:4999/v1",
            ):
                config_text, auth_text, _config, _auth = codex_config.apply_structured_patch(
                    config_text,
                    "{}\n",
                    {
                        "litellm_model": {
                            "model": "active-chat",
                            "provider": "active",
                            "deployment_id": "a1b2c3d4",
                        }
                    },
                    runtime_config,
                )

        parsed = tomllib.loads(config_text)
        self.assertEqual("newapi", parsed["model_provider"])
        self.assertEqual("active-chat", parsed["model"])
        self.assertEqual("https://stale-openai.example.test/v1", parsed["openai_base_url"])
        self.assertEqual(codex_config.LITELLM_CODEX_PROVIDER_NAME, parsed["model_providers"]["newapi"]["name"])
        self.assertEqual("http://127.0.0.1:4999/v1", parsed["model_providers"]["newapi"]["base_url"])
        # The LiteLLM route keeps Codex's standalone search, the only web
        # tool the Responses Lite lane can expose.
        self.assertIs(True, parsed["model_providers"]["newapi"]["supports_standalone_web_search"])
        self.assertEqual("https://fallback.example.test/v1", parsed["model_providers"]["newapi"]["base_url1"])
        self.assertEqual("sk-test-local", json.loads(auth_text)["OPENAI_API_KEY"])

    def _runtime_config_text(self, model_info_extra: str = "") -> str:
        return textwrap.dedent(
            f"""
            providers:
              active:
                api_base: https://active.example.test/v1
                api_keys:
                  - name: default
                    value: replace-me
            model_list:
              - model_name: active-chat
                litellm_params:
                  model: openai/gpt-6-astra
                model_info:
                  id: a1b2c3d4
                  provider: active
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
                  {model_info_extra}
            general_settings:
              master_key: sk-test-local
            """
        ).lstrip()

    def _apply_selection(self, runtime_config: Path) -> tuple[str, str]:
        with mock.patch.object(
            codex_config,
            "local_base_url",
            return_value="http://127.0.0.1:4999/v1",
        ):
            config_text, auth_text, _config, _auth = codex_config.apply_structured_patch(
                'model = "previous"\n',
                "{}",
                {
                    "litellm_model": {
                        "model": "active-chat",
                        "provider": "active",
                        "deployment_id": "a1b2c3d4",
                    }
                },
                runtime_config,
            )
        return config_text, auth_text

    def test_litellm_model_selection_uses_openai_name_only_for_explicit_compaction_opt_in(self) -> None:
        """Provider display name follows the per-model capability metadata.

        ``supports_responses_compaction: true`` is the only case allowed to
        advertise the exact ``OpenAI`` name (which makes new Codex tasks use
        encrypted remote compaction); ``false`` and an absent field both keep
        the neutral ``Young Router`` name (local checkpoint summary). The
        upstream model name (gpt-6-astra here) never triggers remote
        compaction by itself.
        """

        for model_info_line, expected_name in (
            ("supports_responses_compaction: true", "OpenAI"),
            ("supports_responses_compaction: false", codex_config.LITELLM_CODEX_PROVIDER_NAME),
            ("", codex_config.LITELLM_CODEX_PROVIDER_NAME),
        ):
            with self.subTest(model_info_line=model_info_line or "missing"):
                with tempfile.TemporaryDirectory() as directory:
                    runtime_config = Path(directory) / "config.yaml"
                    runtime_config.write_text(
                        self._runtime_config_text(model_info_line),
                        encoding="utf-8",
                    )
                    config_text, _auth_text = self._apply_selection(runtime_config)

                parsed = tomllib.loads(config_text)
                provider = parsed["model_providers"][codex_config.LITELLM_CODEX_PROVIDER_ID]
                self.assertEqual(expected_name, provider["name"])
                self.assertEqual("http://127.0.0.1:4999/v1", provider["base_url"])
                self.assertEqual("responses", provider["wire_api"])

    def test_litellm_model_selection_accepts_compaction_capability_alias(self) -> None:
        """The unified alias maps onto the canonical capability.

        An explicit ``supports_compaction: true`` in model_info is the same
        opt-in as the canonical key, so the provider row may use the exact
        OpenAI name.
        """

        with tempfile.TemporaryDirectory() as directory:
            runtime_config = Path(directory) / "config.yaml"
            runtime_config.write_text(
                self._runtime_config_text("supports_compaction: true"),
                encoding="utf-8",
            )
            config_text, _auth_text = self._apply_selection(runtime_config)

        parsed = tomllib.loads(config_text)
        provider = parsed["model_providers"][codex_config.LITELLM_CODEX_PROVIDER_ID]
        self.assertEqual("OpenAI", provider["name"])

    def test_configured_models_names_the_route_key_and_order(self) -> None:
        """A surface that lists saved routes can tell two of one public name apart."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    providers:
                      relay:
                        api_base: "https://relay.example.test/v1"
                        api_keys:
                          - name: alpha
                            value: "replace-alpha"
                          - name: beta
                            value: "replace-beta"
                    model_list:
                      - model_name: shared-model
                        litellm_params:
                          model: openai/shared-model
                          api_base: "https://relay.example.test/v1"
                          api_key: "replace-alpha"
                          order: 0.3
                        model_info:
                          id: a1b2c3d4
                          provider: relay
                          api_key_name: alpha
                      - model_name: shared-model
                        litellm_params:
                          model: openai/shared-model
                          api_base: "https://relay.example.test/v1"
                          api_key: "replace-beta"
                          order: 1
                        model_info:
                          id: e5f6a7b8
                          provider: relay
                          api_key_name: beta
                    """
                ).lstrip()
            )
            config = codex_config.load_yaml(path)

        rows = codex_config.configured_models(config)

        self.assertEqual(["alpha", "beta"], [row["api_key_name"] for row in rows])
        self.assertEqual([0.3, 1], [row["order"] for row in rows])
        self.assertEqual(["a1b2c3d4", "e5f6a7b8"], [row["deployment_id"] for row in rows])

    def test_configured_models_exposes_explicit_compaction_support_only(self) -> None:
        """Configured rows carry the boolean opt-in; inference never happens.

        The provider display name ``OpenAI`` and the gpt-6 upstream model do
        not imply remote compaction support on their own.
        """

        with tempfile.TemporaryDirectory() as directory:
            runtime_config = Path(directory) / "config.yaml"
            runtime_config.write_text(
                textwrap.dedent(
                    """
                    providers:
                      active:
                        api_base: https://active.example.test/v1
                        api_keys:
                          - name: default
                            value: replace-me
                      openai:
                        name: OpenAI
                        api_base: https://active.example.test/v1
                        api_keys:
                          - name: default
                            value: replace-me
                    model_list:
                      - model_name: opted-in
                        litellm_params:
                          model: openai/gpt-6-astra
                        model_info:
                          id: a1b2c3d4
                          provider: active
                          supports_responses_compaction: true
                      - model_name: opted-out
                        litellm_params:
                          model: openai/gpt-6-astra
                        model_info:
                          id: a1b2c3d5
                          provider: active
                          supports_responses_compaction: false
                      - model_name: aliased
                        litellm_params:
                          model: openai/gpt-6-astra
                        model_info:
                          id: a1b2c3d6
                          provider: active
                          supports_compaction: true
                      - model_name: unmarked
                        litellm_params:
                          model: openai/gpt-6-astra
                        model_info:
                          id: a1b2c3d7
                          provider: active
                      - model_name: openai-named
                        litellm_params:
                          model: openai/gpt-6-astra
                        model_info:
                          id: a1b2c3d8
                          provider: openai
                    general_settings:
                      master_key: sk-test-local
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            models = codex_config.configured_models(codex_config.load_yaml(runtime_config))

        by_name = {item["model"]: item for item in models}
        self.assertTrue(by_name["opted-in"]["supports_responses_compaction"])
        self.assertFalse(by_name["opted-out"]["supports_responses_compaction"])
        # The unified alias resolves to the canonical field.
        self.assertTrue(by_name["aliased"]["supports_responses_compaction"])
        # Missing metadata stays None (unknown), never an inferred capability,
        # even when the provider row is literally named OpenAI or the upstream
        # model is a gpt-6 variant.
        self.assertIsNone(by_name["unmarked"]["supports_responses_compaction"])
        self.assertIsNone(by_name["openai-named"]["supports_responses_compaction"])

    def run_command(
        self,
        runtime_config: Path,
        codex_home: Path,
        *arguments: str,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "LITELLM_CONFIG_FILE": str(runtime_config),
                "CODEX_HOME": str(codex_home),
            }
        )
        return subprocess.run(
            [sys.executable, "-m", "young_router.core.codex_config", *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            input=input_text,
            capture_output=True,
            check=False,
        )

    def editor_request(self, config_text: str, auth_text: str, patch: dict | None = None) -> str:
        payload: dict[str, object] = {
            "config_text": config_text,
            "auth_text": auth_text,
        }
        if patch is not None:
            payload["patch"] = patch
        return json.dumps(payload)

    def test_editor_load_reports_existing_permission_conflict_without_hiding_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            config_text = (
                'model = "test-model"\n'
                'sandbox_mode = "workspace-write"\n'
                'default_permissions = ":workspace"\n'
            )
            (codex_home / "config.toml").write_text(config_text, encoding="utf-8")
            (codex_home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-editor-key"}\n', encoding="utf-8")

            result = self.run_command(runtime_config, codex_home, "load")

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(config_text, payload["config_text"])
            self.assertEqual('{"OPENAI_API_KEY": "sk-editor-key"}\n', payload["auth_text"])
            self.assertIn("Permissions conflict", payload["validation_error"])
            self.assertEqual("mixed", payload["structured"]["permissions"]["mode"])

    def test_editor_load_marks_routes_from_disabled_providers_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            (codex_home / "config.toml").write_text('model = "active-chat"\n', encoding="utf-8")
            (codex_home / "auth.json").write_text("{}\n", encoding="utf-8")

            result = self.run_command(runtime_config, codex_home, "load")

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            model_states = {
                model["model"]: model["model_enabled"]
                for model in payload["models"]
            }
            self.assertTrue(model_states["active-chat"])
            self.assertFalse(model_states["disabled-chat"])
            self.assertFalse(model_states["unavailable-chat"])

    def test_editor_sync_invalid_raw_text_returns_safe_zero_exit_payload_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            config_path = codex_home / "config.toml"
            auth_path = codex_home / "auth.json"
            config_path.write_text('personality = "disk"\n', encoding="utf-8")
            auth_path.write_text('{"OPENAI_API_KEY": "disk-key"}\n', encoding="utf-8")
            marker = "sk-do-not-echo-from-parser"
            config_text = f'api_key = "{marker}\n'

            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(config_text, "{}\n"),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(config_text, payload["config_text"])
            self.assertEqual("{}\n", payload["auth_text"])
            self.assertIn("config.toml is not valid TOML", payload["validation_error"])
            self.assertNotIn(marker, payload["validation_error"])
            self.assertEqual('personality = "disk"\n', config_path.read_text(encoding="utf-8"))
            self.assertEqual('{"OPENAI_API_KEY": "disk-key"}\n', auth_path.read_text(encoding="utf-8"))

    def test_editor_sync_round_trips_structured_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            raw_config = textwrap.dedent(
                """
                unknown_setting = "preserve"
                sandbox_mode = "workspace-write"

                [model_providers.previous]
                base_url = "https://previous.example.test/v1"
                wire_api = "responses"

                [mcp_servers.old]
                command = "old-command"
                """
            ).lstrip()
            raw_auth = '{"tokens": {"keep": true}}\n'
            structured_patch = {
                "model": "new-model",
                "review_model": "review-model",
                "model_provider": "custom-provider",
                "openai_base_url": "https://proxy.example.test/v1",
                "api_key": "sk-swift-editor-key",
                "cli_auth_credentials_store": "file",
                "forced_login_method": "api",
                "model_reasoning_effort": "high",
                "plan_mode_reasoning_effort": "medium",
                "model_reasoning_summary": "concise",
                "model_verbosity": "low",
                "personality": "friendly",
                "service_tier": "fast",
                "web_search": "live",
                "model_context_window": "128000",
                "model_auto_compact_token_limit": "80000",
                "tool_output_token_limit": "4096",
                "features": {
                    "fast_mode": True,
                    "goals": True,
                    "js_repl": True,
                    "experimental_use_unified_exec_tool": True,
                    "shell_snapshot": False,
                    "shell_tool": True,
                    "skill_mcp_dependency_install": False,
                    "personality": True,
                },
                "permissions": {
                    "mode": "profile",
                    "default_permissions": ":workspace",
                    "sandbox_mode": None,
                    "approval_policy": None,
                    "network_access": None,
                    "writable_roots": None,
                },
                "providers": [
                    {
                        "id": "custom-provider",
                        "name": "Custom provider",
                        "base_url": "https://upstream.example.test/v1",
                        "wire_api": "responses",
                        "env_key": "",
                        "requires_openai_auth": False,
                        "auth_command": "get-custom-token",
                    }
                ],
                "mcp_servers": [
                    {
                        "id": "project-tools",
                        "enabled": True,
                        "required": False,
                        "transport": "stdio",
                        "command": "project-mcp",
                        "url": "",
                    }
                ],
                "plugins": [{"id": "sample-plugin", "enabled": True}],
                "advanced": {
                    "shell_environment_inherit": "core",
                    "history_persistence": "save-all",
                    "agents_max_threads": "4",
                    "agents_max_depth": "2",
                    "file_opener": "vscode",
                    "mcp_oauth_credentials_store": "keyring",
                },
            }

            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(raw_config, raw_auth, structured_patch),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsNone(payload["validation_error"])
            self.assertEqual("new-model", payload["structured"]["model"])
            self.assertEqual("review-model", payload["structured"]["review_model"])
            self.assertEqual("4096", payload["structured"]["tool_output_token_limit"])
            self.assertEqual("profile", payload["structured"]["permissions"]["mode"])
            provider = next(item for item in payload["structured"]["providers"] if item["id"] == "custom-provider")
            self.assertEqual("get-custom-token", provider["auth_command"])
            server = next(item for item in payload["structured"]["mcp_servers"] if item["id"] == "project-tools")
            self.assertEqual("project-mcp", server["command"])
            self.assertEqual("", server["url"])
            self.assertEqual("core", payload["structured"]["advanced"]["shell_environment_inherit"])
            self.assertEqual("4", payload["structured"]["advanced"]["agents_max_threads"])
            parsed = tomllib.loads(payload["config_text"])
            self.assertEqual("preserve", parsed["unknown_setting"])
            self.assertNotIn("sandbox_mode", parsed)
            self.assertEqual(":workspace", parsed["default_permissions"])
            self.assertEqual("get-custom-token", parsed["model_providers"]["custom-provider"]["auth"]["command"])
            self.assertEqual("project-mcp", parsed["mcp_servers"]["project-tools"]["command"])
            self.assertTrue(parsed["features"]["experimental_use_unified_exec_tool"])
            self.assertEqual("sk-swift-editor-key", json.loads(payload["auth_text"])["OPENAI_API_KEY"])

    def test_editor_apply_returns_raw_text_and_writes_both_files_0600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            config_text = 'model = "applied-model"\n'
            auth_text = '{"OPENAI_API_KEY": "sk-applied-editor-key"}\n'

            result = self.run_command(
                runtime_config,
                codex_home,
                "apply-editor",
                input_text=self.editor_request(config_text, auth_text),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["applied"])
            self.assertEqual(config_text, payload["config_text"])
            self.assertEqual(auth_text, payload["auth_text"])
            config_path = codex_home / "config.toml"
            auth_path = codex_home / "auth.json"
            self.assertEqual(config_text, config_path.read_text(encoding="utf-8"))
            self.assertEqual(auth_text, auth_path.read_text(encoding="utf-8"))
            self.assertEqual(0o600, stat.S_IMODE(config_path.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(auth_path.stat().st_mode))

    def test_editor_apply_rejects_semantic_conflict_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            config_path = codex_home / "config.toml"
            config_path.write_text('personality = "disk"\n', encoding="utf-8")
            conflict = 'sandbox_mode = "workspace-write"\ndefault_permissions = ":workspace"\n'

            result = self.run_command(
                runtime_config,
                codex_home,
                "apply-editor",
                input_text=self.editor_request(conflict, "{}\n"),
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual('personality = "disk"\n', config_path.read_text(encoding="utf-8"))
            self.assertNotIn("workspace-write", result.stderr)

    def test_editor_sync_rejects_provider_semantics_without_secret_echo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            marker = "sk-private-provider-token"
            config_text = textwrap.dedent(
                f"""
                [model_providers.openai]
                wire_api = "chat"

                [model_providers.custom]
                wire_api = "responses"
                env_key = "CUSTOM_KEY"
                experimental_bearer_token = "{marker}"
                """
            ).lstrip()

            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(config_text, "{}\n"),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("reserved built-in provider id", payload["validation_error"])
            self.assertIn("wire_api must be responses", payload["validation_error"])
            self.assertIn("cannot define both env_key and a bearer token", payload["validation_error"])
            self.assertNotIn(marker, payload["validation_error"])

    def test_editor_provider_list_round_trip_preserves_raw_bearer_token(self) -> None:
        """A visible provider edit must not erase a bearer token kept raw-only."""

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            marker = "synthetic-bearer-token-keep"
            config_text = textwrap.dedent(
                f"""
                [model_providers.bearer]
                name = "Old label"
                base_url = "https://upstream.example.test/v1"
                wire_api = "responses"
                experimental_bearer_token = "{marker}"
                """
            ).lstrip()
            request = {
                "providers": [
                    {
                        "id": "bearer",
                        "name": "Updated label",
                        "base_url": "https://upstream.example.test/v1",
                        "wire_api": "responses",
                        "env_key": "",
                        "requires_openai_auth": False,
                        "auth_mode": "bearer",
                        "auth_command": None,
                    }
                ]
            }

            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(config_text, "{}\n", request),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsNone(payload["validation_error"])
            parsed = tomllib.loads(payload["config_text"])
            self.assertEqual("Updated label", parsed["model_providers"]["bearer"]["name"])
            self.assertEqual(marker, parsed["model_providers"]["bearer"]["experimental_bearer_token"])
            provider = payload["structured"]["providers"][0]
            self.assertEqual("bearer", provider["auth_mode"])
            self.assertIsNone(provider["auth_command"])

    def test_editor_minimal_control_patch_preserves_untouched_sections(self) -> None:
        """One structured field must not materialize UI defaults elsewhere."""

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            config_text = textwrap.dedent(
                """
                unknown_setting = "keep"

                [features]
                remote_future_feature = true

                [model_providers.raw-only]
                experimental_bearer_token = "raw-only-token"
                wire_api = "responses"
                """
            ).lstrip()
            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    config_text,
                    "{}\n",
                    {"model": "direct-model"},
                ),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsNone(payload["validation_error"])
            parsed = tomllib.loads(payload["config_text"])
            self.assertEqual("direct-model", parsed["model"])
            self.assertEqual("keep", parsed["unknown_setting"])
            self.assertEqual({"remote_future_feature": True}, parsed["features"])
            self.assertEqual(
                "raw-only-token",
                parsed["model_providers"]["raw-only"]["experimental_bearer_token"],
            )
            self.assertNotIn("sandbox_mode", parsed)
            self.assertNotIn("default_permissions", parsed)

    def test_editor_writes_a_new_surface_feature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    "",
                    "{}\n",
                    {"features": {"future_use": True}},
                )
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["structured"]["features"]["future_use"])
            self.assertTrue(tomllib.loads(payload["config_text"])["features"]["future_use"])

    def test_editor_switches_between_legacy_and_profile_permissions(self) -> None:
        """The mutually exclusive policy keys must be replaced, not merely toggled in UI."""

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            legacy = textwrap.dedent(
                """
                sandbox_mode = "workspace-write"
                approval_policy = "never"

                [sandbox_workspace_write]
                network_access = true
                writable_roots = ["/tmp"]
                """
            ).lstrip()

            to_profile = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    legacy,
                    "{}\n",
                    {
                        "permissions": {
                            "mode": "profile",
                            "default_permissions": ":workspace",
                            "approval_policy": "never",
                        }
                    },
                ),
            )
            self.assertEqual(0, to_profile.returncode, to_profile.stderr)
            profile_text = json.loads(to_profile.stdout)["config_text"]
            profile = tomllib.loads(profile_text)
            self.assertEqual(":workspace", profile["default_permissions"])
            self.assertEqual("never", profile["approval_policy"])
            self.assertNotIn("sandbox_mode", profile)
            self.assertNotIn("sandbox_workspace_write", profile)

            to_legacy = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    profile_text,
                    "{}\n",
                    {
                        "permissions": {
                            "mode": "legacy",
                            "sandbox_mode": "workspace-write",
                            "approval_policy": "never",
                            "network_access": True,
                            "writable_roots": ["/tmp"],
                        }
                    },
                ),
            )
            self.assertEqual(0, to_legacy.returncode, to_legacy.stderr)
            legacy_again = tomllib.loads(json.loads(to_legacy.stdout)["config_text"])
            self.assertEqual("workspace-write", legacy_again["sandbox_mode"])
            self.assertEqual("never", legacy_again["approval_policy"])
            self.assertTrue(legacy_again["sandbox_workspace_write"]["network_access"])
            self.assertEqual(["/tmp"], legacy_again["sandbox_workspace_write"]["writable_roots"])
            self.assertNotIn("default_permissions", legacy_again)

    def test_permission_mode_selector_is_complete_without_hidden_companion_fields(self) -> None:
        """The segmented mode control sends only ``mode`` and must still write a usable config."""

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            legacy = 'sandbox_mode = "workspace-write"\n'

            to_profile = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    legacy,
                    "{}\n",
                    {"permissions": {"mode": "profile"}},
                ),
            )
            self.assertEqual(0, to_profile.returncode, to_profile.stderr)
            profile = tomllib.loads(json.loads(to_profile.stdout)["config_text"])
            self.assertEqual(":workspace", profile["default_permissions"])
            self.assertNotIn("sandbox_mode", profile)

            to_legacy = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    json.loads(to_profile.stdout)["config_text"],
                    "{}\n",
                    {"permissions": {"mode": "legacy"}},
                ),
            )
            self.assertEqual(0, to_legacy.returncode, to_legacy.stderr)
            legacy_again = tomllib.loads(json.loads(to_legacy.stdout)["config_text"])
            self.assertEqual("workspace-write", legacy_again["sandbox_mode"])
            self.assertNotIn("default_permissions", legacy_again)

    def test_editor_direct_connection_writes_the_selected_provider_endpoint(self) -> None:
        """A custom provider must not silently receive an unused openai_base_url."""

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            config_text = textwrap.dedent(
                """
                model = "direct-model"
                model_provider = "relay"
                openai_base_url = "https://openai.example.test/v1"

                [model_providers.relay]
                base_url = "https://old-relay.example.test/v1"
                wire_api = "responses"
                """
            ).lstrip()

            custom = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    config_text,
                    "{}\n",
                    {
                        "direct_connection": {
                            "provider": "relay",
                            "base_url": "https://new-relay.example.test/v1",
                        }
                    },
                ),
            )
            self.assertEqual(0, custom.returncode, custom.stderr)
            custom_config = tomllib.loads(json.loads(custom.stdout)["config_text"])
            self.assertEqual(
                "https://new-relay.example.test/v1",
                custom_config["model_providers"]["relay"]["base_url"],
            )
            self.assertEqual("https://openai.example.test/v1", custom_config["openai_base_url"])

            built_in = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    json.loads(custom.stdout)["config_text"],
                    "{}\n",
                    {
                        "direct_connection": {
                            "provider": "openai",
                            "base_url": "https://new-openai.example.test/v1",
                        }
                    },
                ),
            )
            self.assertEqual(0, built_in.returncode, built_in.stderr)
            built_in_config = tomllib.loads(json.loads(built_in.stdout)["config_text"])
            self.assertEqual("https://new-openai.example.test/v1", built_in_config["openai_base_url"])
            self.assertEqual(
                "https://new-relay.example.test/v1",
                built_in_config["model_providers"]["relay"]["base_url"],
            )

    def test_editor_switches_and_renames_a_custom_provider_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            config_text = textwrap.dedent(
                """
                model = "direct-model"
                model_provider = "relay"

                [model_providers.relay]
                name = "Relay"
                base_url = "https://old-relay.example.test/v1"
                wire_api = "responses"
                """
            ).lstrip()

            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    config_text,
                    "{}\n",
                    {
                        "providers": [
                            {
                                "id": "renamed-relay",
                                "name": "Relay",
                                "base_url": "https://new-relay.example.test/v1",
                                "wire_api": "responses",
                                "auth_mode": "none",
                            }
                        ],
                        "direct_connection": {
                            "provider": "renamed-relay",
                            "base_url": "https://new-relay.example.test/v1",
                        },
                        "model_provider": "renamed-relay",
                    },
                ),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            parsed = tomllib.loads(json.loads(result.stdout)["config_text"])
            self.assertEqual("renamed-relay", parsed["model_provider"])
            self.assertNotIn("relay", parsed["model_providers"])
            self.assertEqual(
                "https://new-relay.example.test/v1",
                parsed["model_providers"]["renamed-relay"]["base_url"],
            )

    def test_editor_apply_refuses_symbolic_link_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            target = temp / "outside.toml"
            target.write_text('personality = "outside"\n', encoding="utf-8")
            (codex_home / "config.toml").symlink_to(target)

            result = self.run_command(
                runtime_config,
                codex_home,
                "apply-editor",
                input_text=self.editor_request('model = "safe"\n', "{}\n"),
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual('personality = "outside"\n', target.read_text(encoding="utf-8"))
            self.assertNotIn("outside", result.stderr)

    def test_editor_apply_rolls_back_auth_when_config_commit_fails(self) -> None:
        from young_router.core import codex_config

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            codex_home = temp / "codex"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            auth_path = codex_home / "auth.json"
            original_config = 'model = "before"\n'
            original_auth = '{"OPENAI_API_KEY": "before-key"}\n'
            config_path.write_text(original_config, encoding="utf-8")
            auth_path.write_text(original_auth, encoding="utf-8")
            real_atomic_write = codex_config.atomic_write

            def fail_second_write(path: Path, data: str, mode: int = 0o600) -> None:
                if path == config_path and 'model = "after"' in data:
                    raise OSError("synthetic failure")
                real_atomic_write(path, data, mode)

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False), \
                    mock.patch.object(codex_config, "atomic_write", side_effect=fail_second_write):
                with self.assertRaises(OSError):
                    codex_config.atomic_write_editor_files(
                        'model = "after"\n',
                        '{"OPENAI_API_KEY": "after-key"}\n',
                    )

            self.assertEqual(original_config, config_path.read_text(encoding="utf-8"))
            self.assertEqual(original_auth, auth_path.read_text(encoding="utf-8"))


class LocalApiKeyTests(unittest.TestCase):
    """A key handed to a client must be the one the proxy will accept.

    LiteLLM looks a non-master key up as a virtual key, needs the database, and
    refuses with "No connected db.".  A placeholder handed to the client
    therefore reads as a database problem while the real cause is the key.
    """

    def test_the_configured_master_key_is_returned(self) -> None:
        key = "sk-lv-Ha7otT0x56GNc9McyJDNtcZ2wWGoykH3NNf6aRH9"
        config = {"general_settings": {"master_key": key}}

        self.assertEqual(codex_config.local_api_key(config), key)

    def test_a_config_without_a_key_is_an_error_not_a_placeholder(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LITELLM_MASTER_KEY", None)
            with self.assertRaises(codex_config.EditorError):
                codex_config.local_api_key({"general_settings": {}})

    def test_an_env_reference_the_client_cannot_resolve_is_an_error(self) -> None:
        config = {"general_settings": {"master_key": "os.environ/NOT_SET_ANYWHERE"}}

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOT_SET_ANYWHERE", None)
            with self.assertRaises(codex_config.EditorError):
                codex_config.local_api_key(config)

    def test_the_env_var_is_used_when_the_config_names_none(self) -> None:
        with mock.patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-env-key"}):
            self.assertEqual(
                codex_config.local_api_key({"general_settings": {}}), "sk-env-key"
            )


class ProviderDocumentParseCacheTests(unittest.TestCase):
    """One request reads the provider document many times; parse it once.

    The Codex editor's runtime context, its saved-route list, the local proxy
    endpoint, the runtime domain's master key, and the service port each read
    ``config.yaml`` while one settings commit runs.  Every one of those used to
    pay a full PyYAML parse of the same bytes, which is what put a
    quarter-second parse behind a single click.

    The parse is shared through ``config.schema``'s cache, the one this app's
    editor, dumper, and loader already use, so a document validated by the
    settings editor and then read by the Codex pane costs one parse in total.
    """

    def setUp(self) -> None:
        from young_router.config import schema as config_schema

        # A cache carried over from another test would make this pass without
        # the read it is measuring ever happening.
        self._schema = config_schema
        self._saved = dict(config_schema._yaml_text_parsed)
        config_schema._yaml_text_parsed.clear()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        self._schema._yaml_text_parsed.clear()
        self._schema._yaml_text_parsed.update(self._saved)

    def test_unchanged_bytes_are_parsed_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("model_list:\n  - model_name: one\n", encoding="utf-8")
            with mock.patch.object(
                self._schema, "_yaml_load", wraps=self._schema._yaml_load
            ) as parse:
                first = codex_config.load_yaml(path)
                second = codex_config.load_yaml(path)
            self.assertEqual(1, parse.call_count)
            self.assertEqual(first, second)

    def test_each_caller_owns_the_mapping_it_receives(self) -> None:
        """The cached tree is shared; a caller's mutation must stay its own."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("model_list:\n  - model_name: one\n", encoding="utf-8")
            first = codex_config.load_yaml(path)
            first["model_list"].append({"model_name": "injected"})
            first["extra"] = True

            second = codex_config.load_yaml(path)

            self.assertEqual([{"model_name": "one"}], second["model_list"])
            self.assertNotIn("extra", second)

    def test_a_rewritten_document_is_parsed_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("model_list:\n  - model_name: one\n", encoding="utf-8")
            self.assertEqual("one", codex_config.load_yaml(path)["model_list"][0]["model_name"])

            path.write_text("model_list:\n  - model_name: two\n", encoding="utf-8")

            self.assertEqual("two", codex_config.load_yaml(path)["model_list"][0]["model_name"])

    def test_an_invalid_document_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("model_list: [\n", encoding="utf-8")
            with self.assertRaises(codex_config.EditorError):
                codex_config.load_yaml(path)
            path.write_text("- not a mapping\n", encoding="utf-8")
            with self.assertRaises(codex_config.EditorError):
                codex_config.load_yaml(path)

    def test_the_master_key_reuses_the_codex_editors_parse(self) -> None:
        """The runtime domain reads the key out of the document Codex parsed."""

        from young_router.core.domains.runtime import master_key_from_document

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "general_settings:\n  master_key: sk-replace-cache\n",
                encoding="utf-8",
            )
            text = path.read_text(encoding="utf-8")
            codex_config.load_yaml(path)
            with mock.patch.object(
                self._schema, "_yaml_load", side_effect=AssertionError("re-parsed")
            ):
                self.assertEqual("sk-replace-cache", master_key_from_document(text))

    def test_a_document_this_process_never_read_still_parses(self) -> None:
        from young_router.core.domains.runtime import master_key_from_document

        self.assertEqual(
            "sk-replace-unseen",
            master_key_from_document(
                "general_settings:\n  master_key: sk-replace-unseen\n"
            ),
        )
        self.assertEqual(
            "sk-young-router", master_key_from_document("general_settings: {}\n")
        )
        self.assertEqual("sk-young-router", master_key_from_document(""))


if __name__ == "__main__":
    unittest.main()


class LocalPortTests(unittest.TestCase):
    """The client document must name the port the proxy is listening on.

    ``LITELLM_PORT`` is the preference, and an occupied port makes the managed
    service step forward.  Writing the client's ``base_url`` from the
    preference then hands the client a dead address, which reads as a silent
    turn: no thinking, no answer, nothing in the request log (observed live
    2026-11-04, a leftover benchmark proxy on 12390 while the install ran on
    12391).
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.runtime = self.root / ".litellm-runtime"
        self.runtime.mkdir(parents=True)
        self._env = mock.patch.dict(
            os.environ,
            {"LITELLM_RUNTIME_ROOT": str(self.root), "LITELLM_PORT": "12390"},
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._directory.cleanup()

    def _record(self, pid: int, port: int) -> None:
        (self.runtime / codex_config.MANAGED_OWNER_FILENAME).write_text(
            json.dumps({"pid": pid, "token": "t"}), encoding="utf-8"
        )
        (self.runtime / codex_config.MANAGED_PORT_FILENAME).write_text(
            str(port), encoding="utf-8"
        )

    def test_a_running_service_moves_the_client_onto_its_bound_port(self) -> None:
        self._record(os.getpid(), 12391)
        self.assertEqual("12391", codex_config.local_port())
        self.assertEqual("http://127.0.0.1:12391/v1", codex_config.local_base_url())

    def test_a_dead_service_does_not_pin_the_preference(self) -> None:
        self._record(999_999_999, 12391)
        self.assertEqual("12390", codex_config.local_port())

    def test_no_records_means_the_preference(self) -> None:
        self.assertEqual("12390", codex_config.local_port())
