from __future__ import annotations

import base64
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "service.sh"
COMMON_MODELS_SOURCE = ROOT / "mac_menu" / "Sources" / "CommonModels.swift"
RUNTIME_SETTINGS_DIALOG_SOURCE = ROOT / "mac_menu" / "Sources" / "RuntimeSettingsDialog.swift"
HARNESS_SOURCE = ROOT / "tests" / "fixtures" / "runtime_settings_dialog" / "main.swift"


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("swiftc"),
    "Runtime Settings AppKit tests require macOS and swiftc.",
)
class RuntimeSettingsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.temp = Path(cls.temp_dir.name)
        cls.harness = cls.temp / "runtime-settings-dialog-harness"
        cls.home = cls.temp / "home"
        cls.home.mkdir()
        cls.payload = cls.load_runtime_settings_payload()

        compiled = subprocess.run(
            [
                "swiftc",
                str(COMMON_MODELS_SOURCE),
                str(RUNTIME_SETTINGS_DIALOG_SOURCE),
                str(HARNESS_SOURCE),
                "-o",
                str(cls.harness),
                "-framework",
                "Cocoa",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if compiled.returncode != 0:
            raise AssertionError(
                "Could not compile Runtime Settings AppKit harness:\n"
                + compiled.stdout
                + compiled.stderr
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    @classmethod
    def checkout_env(cls, runtime_name: str) -> dict[str, str]:
        runtime_root = cls.temp / runtime_name
        runtime_root.mkdir(exist_ok=True)
        return {
            "HOME": str(cls.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
            "LITELLM_ALLOW_CHECKOUT_SERVICE": "1",
            "LITELLM_RUNTIME_ROOT": str(runtime_root),
            "LITELLM_TEMPLATE_ROOT": str(ROOT),
        }

    @classmethod
    def load_runtime_settings_payload(cls) -> dict[str, object]:
        loaded = subprocess.run(
            ["/bin/bash", str(CONTROL), "runtime-settings"],
            cwd=ROOT,
            env=cls.checkout_env("payload-runtime"),
            text=True,
            capture_output=True,
            check=False,
        )
        if loaded.returncode != 0:
            raise AssertionError(
                "Could not load isolated Runtime Settings payload:\n"
                + loaded.stdout
                + loaded.stderr
            )
        return json.loads(loaded.stdout)

    def run_harness(
        self,
        *arguments: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        result = subprocess.run(
            [str(self.harness), *arguments],
            cwd=ROOT,
            input=json.dumps(payload or self.payload),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def frontend_accepts(self, values: dict[str, str]) -> bool:
        encoded = base64.b64encode(json.dumps(values).encode("utf-8")).decode("ascii")
        result = self.run_harness("validate", encoded)
        return bool(result["valid"])

    def backend_accepts(self, values: dict[str, str], case_number: int) -> bool:
        configured = subprocess.run(
            ["/bin/bash", str(CONTROL), "runtime-settings-configure"],
            cwd=ROOT,
            env=self.checkout_env(f"validation-runtime-{case_number}"),
            input=json.dumps({"values": values}),
            text=True,
            capture_output=True,
            check=False,
        )
        return configured.returncode == 0

    def test_document_tracks_dynamic_content_without_blank_scroll_tail(self) -> None:
        full = self.run_harness("layout", "900")
        compact_payload = copy.deepcopy(self.payload)
        compact_payload["settings"] = compact_payload["settings"][:1]
        compact = self.run_harness("layout", "900", payload=compact_payload)
        partial_payload = copy.deepcopy(self.payload)
        partial_payload["settings"] = partial_payload["settings"][:12]
        partial = self.run_harness("layout", "900", payload=partial_payload)

        for name, layout in (("compact", compact), ("partial", partial), ("full", full)):
            with self.subTest(size=name):
                self.assertAlmostEqual(
                    layout["document_width"],
                    layout["clip_width"],
                    delta=1.0,
                )
                self.assertAlmostEqual(layout["left_inset"], 12.0, delta=1.0)
                self.assertAlmostEqual(layout["right_inset"], 12.0, delta=1.0)
                self.assertAlmostEqual(layout["top_inset"], 10.0, delta=1.0)
                self.assertGreaterEqual(layout["document_height"], layout["clip_height"])

        self.assertLess(compact["stack_height"], partial["stack_height"])
        self.assertLess(partial["stack_height"], full["stack_height"])
        self.assertAlmostEqual(
            compact["document_height"],
            compact["clip_height"],
            delta=1.0,
            msg="A short form must not have a scrollable blank tail.",
        )
        for name, layout in (("partial", partial), ("full", full)):
            with self.subTest(size=name):
                self.assertAlmostEqual(layout["bottom_inset"], 10.0, delta=1.0)
                self.assertAlmostEqual(
                    layout["document_height"],
                    layout["stack_height"] + 20.0,
                    delta=1.0,
                    msg="The document height must derive from its actual rows.",
                )

    def test_every_runtime_row_uses_the_shared_alignment_grid(self) -> None:
        def boundary(frame: dict[str, float]) -> tuple[float, float]:
            return (frame["min_x"], frame["max_x"])

        for width in (900,):
            with self.subTest(width=width):
                audit = self.run_harness("alignment", str(width))
                entries = audit["entries"]
                self.assertEqual(len(entries), len(self.payload["settings"]))

                stack = audit["form_stack"]
                expected_columns = {
                    name: boundary(entries[0][name])
                    for name in ("label", "value_slot", "action_slot", "unit")
                }

                for entry in entries:
                    with self.subTest(width=width, key=entry["key"]):
                        self.assertAlmostEqual(
                            entry["row"]["min_x"], stack["min_x"], delta=1.0
                        )
                        self.assertAlmostEqual(
                            entry["row"]["max_x"], stack["max_x"], delta=1.0
                        )
                        self.assertAlmostEqual(
                            entry["input_row"]["min_x"],
                            entry["row"]["min_x"],
                            delta=1.0,
                        )
                        self.assertAlmostEqual(
                            entry["input_row"]["max_x"],
                            entry["row"]["max_x"],
                            delta=1.0,
                        )

                        for name, expected in expected_columns.items():
                            actual = boundary(entry[name])
                            self.assertAlmostEqual(actual[0], expected[0], delta=1.0)
                            self.assertAlmostEqual(actual[1], expected[1], delta=1.0)

                        self.assertAlmostEqual(
                            entry["help"]["min_x"],
                            entry["value_slot"]["min_x"],
                            delta=1.0,
                        )
                        for name in ("label", "control", "unit"):
                            self.assertAlmostEqual(
                                entry[name]["mid_y"],
                                entry["input_row"]["mid_y"],
                                delta=1.0,
                            )

    def test_all_runtime_controls_are_accessible_and_api_key_is_secure(self) -> None:
        payload = copy.deepcopy(self.payload)
        settings = payload["settings"]
        self.assertEqual(len(settings), 48)
        api_key_item = next(
            item
            for item in settings
            if item["key"] == "LITELLM_MENU_VISION_BRIDGE_API_KEY"
        )
        api_key_item["value"] = "synthetic-secret"
        api_key_item["configured"] = True

        audit = self.run_harness("controls", payload=payload)
        self.assertEqual(audit["settings_count"], len(settings))
        self.assertEqual(audit["fields_count"], len(settings))
        entries = {entry["key"]: entry for entry in audit["entries"]}
        self.assertEqual(set(entries), {item["key"] for item in settings})

        for item in settings:
            with self.subTest(key=item["key"]):
                accessibility_label = entries[item["key"]]["accessibility_label"]
                self.assertTrue(accessibility_label.strip())
                self.assertIn(item["label"], accessibility_label)
        self.assertEqual(
            len({entry["accessibility_label"] for entry in entries.values()}),
            len(settings),
            "Runtime controls need distinct accessibility labels.",
        )

        self.assertTrue(
            entries["LITELLM_MENU_VISION_BRIDGE_API_KEY"]["is_secure"],
            "API keys must use NSSecureTextField rather than a plain text field.",
        )
        self.assertFalse(
            entries["LITELLM_MENU_VISION_BRIDGE_API_KEY"]["has_visible_text"],
            "A Runtime Settings payload must never place an API key into the field.",
        )

    def test_configured_api_key_supports_retain_replace_and_clear(self) -> None:
        payload = copy.deepcopy(self.payload)
        api_key_item = next(
            item
            for item in payload["settings"]
            if item["key"] == "LITELLM_MENU_VISION_BRIDGE_API_KEY"
        )
        retain_marker = "__LITELLM_MENU_RETAIN_EXISTING__"
        api_key_item["value"] = retain_marker
        api_key_item["configured"] = True
        api_key_item["secret"] = True
        api_key_item["retain_existing"] = retain_marker

        audit = self.run_harness("secret", payload=payload)

        self.assertEqual(audit["initial_display"], "")
        self.assertIn("configured", audit["initial_placeholder"].lower())
        self.assertEqual(audit["untouched_value"], retain_marker)
        self.assertEqual(audit["replacement_value"], "synthetic-replacement")
        self.assertEqual(audit["cleared_value"], "")

    def test_frontend_validation_matches_backend_contract(self) -> None:
        cases = [
            ("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "4", True),
            ("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "20", True),
            ("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "007", True),
            ("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", " 7 ", True),
            ("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "+1", False),
            ("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "-1", False),
            ("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "1.0", False),
            ("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "1e1", False),
            ("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "0", False),
            ("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "21", False),
            ("LITELLM_CONFIG_WATCH_INTERVAL", "0.2", True),
            ("LITELLM_CONFIG_WATCH_INTERVAL", "1", True),
            ("LITELLM_CONFIG_WATCH_INTERVAL", "300", True),
            ("LITELLM_CONFIG_WATCH_INTERVAL", " 1.5 ", True),
            ("LITELLM_CONFIG_WATCH_INTERVAL", "+1", False),
            ("LITELLM_CONFIG_WATCH_INTERVAL", ".5", False),
            ("LITELLM_CONFIG_WATCH_INTERVAL", "1.", False),
            ("LITELLM_CONFIG_WATCH_INTERVAL", "1e2", False),
            ("LITELLM_CONFIG_WATCH_INTERVAL", "nan", False),
            ("LITELLM_CONFIG_WATCH_INTERVAL", "0.1", False),
            ("LITELLM_CONFIG_WATCH_INTERVAL", "301", False),
            ("LITELLM_MENU_LOG_MAX_BYTES", "0.25", True),
            ("LITELLM_MENU_LOG_MAX_BYTES", "100", True),
            ("LITELLM_MENU_LOG_MAX_BYTES", ".25", False),
            ("LITELLM_MENU_LOG_MAX_BYTES", "100.1", False),
            ("LITELLM_MENU_VISION_BRIDGE_PROMPT", "Synthetic prompt", True),
            ("LITELLM_MENU_VISION_BRIDGE_PROMPT", "line one\nline two", False),
            ("LITELLM_MENU_VISION_BRIDGE_PROMPT", "line one\rline two", False),
            ("LITELLM_MENU_VISION_BRIDGE_PROMPT", "value#fragment", False),
            ("LITELLM_MENU_WEB_SEARCH_REGION", "cn-zh", True),
            ("LITELLM_MENU_WEB_SEARCH_REGION", "cn zh", False),
            ("LITELLM_MENU_WEB_SEARCH_REGION", "cn\tzh", False),
        ]

        for case_number, (key, value, expected) in enumerate(cases):
            with self.subTest(key=key, value=repr(value)):
                values = {key: value}
                frontend = self.frontend_accepts(values)
                backend = self.backend_accepts(values, case_number)
                self.assertEqual(backend, expected, "Backend contract changed unexpectedly.")
                self.assertEqual(
                    frontend,
                    backend,
                    "Native validation must reject values before closing the editor.",
                )

    def test_cross_field_validation_matches_backend_after_patch_merge(self) -> None:
        cases = [
            (
                {
                    "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS": "3",
                    "LITELLM_MENU_WEB_SEARCH_READ_RESULTS": "3",
                },
                True,
            ),
            (
                {
                    "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS": "3",
                    "LITELLM_MENU_WEB_SEARCH_READ_RESULTS": "4",
                },
                False,
            ),
            ({"LITELLM_MENU_WEB_SEARCH_MAX_RESULTS": "3"}, False),
            ({"LITELLM_MENU_WEB_SEARCH_READ_RESULTS": "9"}, False),
        ]

        for case_number, (values, expected) in enumerate(cases, start=100):
            with self.subTest(values=values):
                frontend = self.frontend_accepts(values)
                backend = self.backend_accepts(values, case_number)
                self.assertEqual(backend, expected, "Backend cross-field contract changed.")
                self.assertEqual(frontend, backend)


if __name__ == "__main__":
    unittest.main()
