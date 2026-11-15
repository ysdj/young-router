"""Edge cases for the runtime domain's master-key document handling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from young_router.core.domains.runtime import (
    RuntimeSettingsDomain,
    master_key_from_document,
    master_key_in_document,
    master_key_is_valid,
)


class MasterKeyEditingEdgeTests(unittest.TestCase):
    def test_a_flow_mapping_header_becomes_a_block_and_reads_back(self) -> None:
        updated = master_key_in_document("general_settings: {}\nproviders: {}\n", "sk-new")

        self.assertEqual(master_key_from_document(updated), "sk-new")
        self.assertIn("general_settings:\n  master_key: sk-new\n", updated)

    def test_a_last_line_without_a_newline_is_replaced_not_duplicated(self) -> None:
        updated = master_key_in_document("general_settings:\n  master_key: old", "sk-new")

        self.assertEqual(updated, "general_settings:\n  master_key: sk-new\n")

    def test_a_key_yaml_would_resolve_differently_is_refused(self) -> None:
        for candidate in ("123", "on", "null", "~", "@abc"):
            with self.subTest(candidate=candidate):
                self.assertFalse(master_key_is_valid(candidate))
        self.assertTrue(master_key_is_valid("sk-123"))
        self.assertTrue(master_key_is_valid("AbC+/=_-"))


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
