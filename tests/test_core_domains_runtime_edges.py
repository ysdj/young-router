"""Edge cases for the runtime domain's master-key document handling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from young_router.core.domains.runtime import (
    MASTER_KEY_SETTING,
    RuntimeSettingsDomain,
    master_key_from_document,
    master_key_in_document,
    master_key_is_valid,
)


class MasterKeyEditingEdgeTests(unittest.TestCase):
    def test_a_flow_mapping_keeps_its_contents_comments_and_aliases(self) -> None:
        updated = master_key_in_document("general_settings: {}\nproviders: {}\n", "sk-new")

        self.assertEqual(master_key_from_document(updated), "sk-new")
        self.assertEqual(yaml.safe_load(updated)["general_settings"], {"master_key": "sk-new"})

        source = (
            "defaults: &defaults {setting: yes}\n"
            "general_settings: {ui: false, nested: *defaults, master_key: old} # keep\n"
            "providers: {}\n"
        )
        updated = master_key_in_document(source, "sk-new,part")
        self.assertEqual(
            yaml.safe_load(updated)["general_settings"],
            {"ui": False, "nested": {"setting": True}, "master_key": "sk-new,part"},
        )
        self.assertEqual(updated, source.replace("master_key: old", 'master_key: "sk-new,part"'))

    def test_a_last_line_without_a_newline_is_replaced_not_duplicated(self) -> None:
        updated = master_key_in_document("general_settings:\n  master_key: old", "sk-new")

        self.assertEqual(updated, "general_settings:\n  master_key: sk-new\n")

    def test_a_key_yaml_would_resolve_differently_is_refused(self) -> None:
        for candidate in ("123", "on", "null", "~", "@abc"):
            with self.subTest(candidate=candidate):
                self.assertFalse(master_key_is_valid(candidate))
        self.assertTrue(master_key_is_valid("sk-123"))
        self.assertTrue(master_key_is_valid("AbC+/=_-"))

    def test_apply_rotates_the_effective_key_and_keeps_other_yaml_bytes(self) -> None:
        for source, to_replace, replacement in (
            (
                '"general_settings":\r\n  "master_key": \'old\' # keep\r\n  ui: false\r\nproviders: {}\r\n',
                "'old'", "'sk-new,part'",
            ),
            (
                "general_settings: {\n  ui: false,\n  nested: [{master_key: other}],\n  master_key: old,\n}\n",
                "master_key: old", 'master_key: "sk-new,part"',
            ),
            (
                "general_settings:\n  subgroup:\n    master_key: nested-old\n\n  master_key: old # keep\n  ui: false\n",
                "master_key: old", "master_key: sk-new,part",
            ),
            (
                "settings: &old old\ngeneral_settings:\n  ui: true\n  master_key: *old # keep\n",
                "master_key: *old", "master_key: sk-new,part",
            ),
            (
                "general_settings:\n  master_key: |- # keep\n    old\n  ui: true\n",
                "|- # keep\n    old", "sk-new,part # keep",
            ),
        ):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "config.yaml"
                config.write_bytes(source.encode("utf-8"))
                domain = RuntimeSettingsDomain(root / "runtime-settings.env", config_path=config)
                self.assertEqual(domain.trusted_secret_value("setting", MASTER_KEY_SETTING), "old")

                domain.stage_secret("setting", MASTER_KEY_SETTING, "sk-new,part")
                applied = domain.apply()

                self.assertTrue(applied["master_key_changed"])
                self.assertEqual(config.read_bytes(), source.replace(to_replace, replacement).encode("utf-8"))
                self.assertEqual(domain.trusted_secret_value("setting", MASTER_KEY_SETTING), "sk-new,part")


class RuntimeDocumentDeletionTests(unittest.TestCase):
    def test_deleting_the_document_is_reported_as_an_external_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text("general_settings:\n  master_key: sk-old\n", encoding="utf-8")
            domain = RuntimeSettingsDomain(root / "runtime-settings.env", config_path=config)
            config.unlink()

            self.assertTrue(domain.external_disk_state()["changed"])


if __name__ == "__main__":
    unittest.main()
