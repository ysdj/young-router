"""One installation has one configuration document.

The failure these tests exist for is not a wrong path; it is two paths.  The
provider editor wrote ``<root>/.litellm-runtime/config.yaml`` while the WebDAV
bundle, the usage-log reader, the Codex editor's default and the JSON config API
each derived ``<root>/config.yaml`` on their own.  Nothing crashed: the editor
saved one document and every other reader believed another, so a rename never
reached the proxy, and the stale copy looked exactly like configuration.

The hold here is that the resolver is the only place a default is decided, and
that a retired document is reported rather than read.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from young_router import config_paths


class ProviderConfigPathTests(unittest.TestCase):
    def test_the_default_is_the_document_the_provider_editor_writes(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LITELLM_CONFIG_FILE", None)
            self.assertEqual(
                Path.home() / ".young-router" / ".litellm-runtime" / "config.yaml",
                config_paths.provider_config_path(),
            )

    def test_an_installation_root_answers_for_itself(self) -> None:
        self.assertEqual(
            Path("/tmp/installation/.litellm-runtime/config.yaml"),
            config_paths.provider_config_path("/tmp/installation"),
        )

    def test_an_explicit_document_wins(self) -> None:
        # A packaged preview profile names its own document.  That setting is
        # one path for the whole process, so it cannot create a second one.
        with mock.patch.dict(
            os.environ, {"LITELLM_CONFIG_FILE": "/tmp/preview/config.yaml"}
        ):
            self.assertEqual(
                Path("/tmp/preview/config.yaml"), config_paths.provider_config_path()
            )

    def test_every_reader_resolves_the_same_document(self) -> None:
        """The whole point, stated once against every caller.

        Each of these used to derive the root-level path on its own.  If a new
        caller grows a private default, this test is where it shows up as two
        different answers.
        """

        from young_router.config.schema import CONFIG_YAML
        from young_router.core import codex_config, remote_usage_logs
        from young_router.core.domains._shared import _default_provider_config_path
        from young_router.core.operations import RuntimePaths
        from young_router.proxy.reasoning import _runtime_config_path

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LITELLM_CONFIG_FILE", None)
            os.environ.pop("LITELLM_RUNTIME_ROOT", None)
            os.environ.pop("YOUNG_ROUTER_HOME", None)
            answers = {
                "config.schema": CONFIG_YAML,
                "codex_config": codex_config.default_config_path(),
                "remote_usage_logs": remote_usage_logs.default_config_path(),
                "domains._shared": _default_provider_config_path(),
                "proxy.reasoning": _runtime_config_path(),
                "operations.paths.config": RuntimePaths.from_root().config,
            }
        self.assertEqual(
            1, len({str(path) for path in answers.values()}), answers
        )

    def test_a_retired_root_document_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            retired = root / "config.yaml"
            retired.write_text("model_list: []\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LITELLM_CONFIG_FILE", None)
                self.assertEqual(retired, config_paths.stale_document(root))

    def test_an_installation_without_a_retired_copy_reports_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LITELLM_CONFIG_FILE", None)
                self.assertIsNone(config_paths.stale_document(Path(directory)))

    def test_an_explicit_document_makes_the_root_level_name_legitimate(self) -> None:
        """A chosen document may itself be the root-level file.

        Reporting it as retired there would invite the user to move the file the
        profile deliberately named.
        """

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"LITELLM_CONFIG_FILE": str(Path(directory) / "config.yaml")}
        ):
            self.assertIsNone(config_paths.stale_document(Path(directory)))


class StagingRefusesARetiredDocumentTests(unittest.TestCase):
    """A file nothing writes must not be able to steer the one everything reads.

    The retired copy is the file the old defaults made staging read.  It is now
    named in the failure instead, so a user whose installation still has one is
    told where their configuration went instead of watching an edit vanish.
    """

    def test_staging_says_which_file_is_live_and_which_is_retired(self) -> None:
        from young_router.core.operations import CoreServiceController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            retired = root / "config.yaml"
            retired.write_text("model_list: []\n", encoding="utf-8")
            controller = CoreServiceController(root)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LITELLM_CONFIG_FILE", None)
                with self.assertRaises(RuntimeError) as raised:
                    controller._stage_runtime_config()
        self.assertIn(str(root / ".litellm-runtime" / "config.yaml"), str(raised.exception))
        self.assertIn(str(retired), str(raised.exception))

    def test_staging_stays_quiet_when_there_is_no_retired_copy(self) -> None:
        from young_router.core.operations import CoreServiceController

        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LITELLM_CONFIG_FILE", None)
                with self.assertRaises(RuntimeError) as raised:
                    controller._stage_runtime_config()
        self.assertEqual(
            "Provider/model configuration is unavailable", str(raised.exception)
        )


if __name__ == "__main__":
    unittest.main()


class ProviderChannelTagTests(unittest.TestCase):
    """A provider's channel is a stored tag, not a reading of its name.

    The name is the user's to change, so deriving the channel from it made a
    rename silently retarget the row: a Kilo entry renamed to anything else read
    back as the anonymous channel and its models were then routed over a wire
    that channel does not serve.
    """

    def test_an_unknown_tag_is_refused_and_the_empty_one_is_kept(self) -> None:
        from young_router.config.schema import _provider_channel

        self.assertEqual(_provider_channel(""), "")
        self.assertEqual(_provider_channel(None), "")
        self.assertEqual(_provider_channel(" Kilo "), "kilo")
        for rejected in (5, "a b", "x" * 40, ["kilo"]):
            with self.assertRaises(ValueError):
                _provider_channel(rejected)
