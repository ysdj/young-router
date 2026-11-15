"""Edge cases for reading and validating the runtime settings file."""

from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest import mock

from young_router.core import runtime_settings_io
from young_router.core.runtime_settings_io import RuntimeSettingSpec


SPECS = {
    "YOUNG_ROUTER_TEST_COUNT": RuntimeSettingSpec(
        key="YOUNG_ROUTER_TEST_COUNT",
        kind="int",
        default="1",
        minimum=0,
        maximum=100,
        options=(),
    ),
    "YOUNG_ROUTER_TEST_BUDGET": RuntimeSettingSpec(
        key="YOUNG_ROUTER_TEST_BUDGET",
        kind="mb",
        default="2",
        minimum=1,
        maximum=16,
        options=(),
    ),
}
DOCUMENT = "YOUNG_ROUTER_TEST_COUNT=5\nYOUNG_ROUTER_TEST_BUDGET=4194304\n"


class ReadConfiguredSettingsFileTests(unittest.TestCase):
    def test_configured_read_parses_the_document_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            path.write_text(DOCUMENT, encoding="utf-8")
            with mock.patch.object(
                runtime_settings_io,
                "read_limited_utf8",
                wraps=runtime_settings_io.read_limited_utf8,
            ) as reads:
                configured = runtime_settings_io.read_configured_settings_file(path, SPECS)
        self.assertEqual(
            configured,
            {"YOUNG_ROUTER_TEST_COUNT": "5", "YOUNG_ROUTER_TEST_BUDGET": "4194304"},
        )
        self.assertEqual(reads.call_count, 1)


class ValidateValuesTests(unittest.TestCase):
    def test_validate_values_normalizes_only_the_supplied_values(self) -> None:
        specs = runtime_settings_io.load_specs()
        spec = next(item for item in specs.values() if item.kind == "int")
        expected = runtime_settings_io.normalize_payload_value(spec, spec.default)
        with mock.patch.object(
            runtime_settings_io,
            "normalize_payload_value",
            wraps=runtime_settings_io.normalize_payload_value,
        ) as normalize:
            normalized = runtime_settings_io.validate_values({spec.key: spec.default}, specs)
        self.assertEqual(normalized, {spec.key: expected})
        self.assertEqual(normalize.call_count, 1)


if __name__ == "__main__":
    unittest.main()
