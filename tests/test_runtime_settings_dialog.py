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
SETTINGS_WINDOW_PRESENTATION_SOURCE = ROOT / "mac_menu" / "Sources" / "SettingsWindowPresentation.swift"
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
                str(SETTINGS_WINDOW_PRESENTATION_SOURCE),
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
            "LITELLM_RUNTIME_ROOT": str(runtime_root),
            "LITELLM_TEMPLATE_ROOT": str(ROOT),
            "LITELLM_PORT": "49240",
            "LITELLM_APP_LAUNCH_AGENT_LABEL": "menu.litellm.menu-login.runtime-dialog-test",
            "LITELLM_CONFIG_WATCH_LABEL": "menu.litellm.config-watch.runtime-dialog-test",
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

    def test_background_save_completion_runs_inside_modal_loop(self) -> None:
        audit = self.run_harness("modal-save")

        self.assertTrue(audit["saved"])
        self.assertTrue(audit["save_in_flight"])

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
        full = self.run_harness("layout", "1120")
        compact_payload = copy.deepcopy(self.payload)
        compact_payload["settings"] = compact_payload["settings"][:1]
        compact = self.run_harness("layout", "1120", payload=compact_payload)
        partial_payload = copy.deepcopy(self.payload)
        partial_payload["settings"] = partial_payload["settings"][:12]
        partial = self.run_harness("layout", "1120", payload=partial_payload)

        for name, layout in (("compact", compact), ("partial", partial), ("full", full)):
            with self.subTest(size=name):
                self.assertAlmostEqual(
                    layout["document_width"],
                    layout["clip_width"],
                    delta=1.0,
                )
                self.assertAlmostEqual(layout["left_inset"], 16.0, delta=1.0)
                self.assertAlmostEqual(layout["right_inset"], 16.0, delta=1.0)
                self.assertAlmostEqual(layout["top_inset"], 14.0, delta=1.0)
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
                self.assertAlmostEqual(layout["bottom_inset"], 14.0, delta=1.0)
                self.assertAlmostEqual(
                    layout["document_height"],
                    layout["stack_height"] + 28.0,
                    delta=1.0,
                    msg="The document height must derive from its actual rows.",
                )

    def test_window_and_help_area_use_a_compact_per_category_runtime_settings_layout(self) -> None:
        metrics = self.run_harness("window-metrics")

        self.assertAlmostEqual(metrics["content_width"], 1080.0, delta=1.0)
        self.assertAlmostEqual(metrics["minimum_width"], 760.0, delta=1.0)
        self.assertAlmostEqual(metrics["maximum_width"], 1160.0, delta=1.0)
        for heading in metrics["headings"]:
            self.assertAlmostEqual(
                heading["min_x"],
                28.0,
                delta=1.0,
                msg="Category headings must start at the form's left edge, not the row-label grid.",
            )
        self.assertTrue(
            any(columns == 2 for columns in metrics["category_columns"]),
            "A wide Runtime Settings window should use two columns inside multi-row categories.",
        )
        self.assertGreater(
            metrics["help_width"],
            250.0,
            "Tips need a practical readable line within their own category column.",
        )
        self.assertLess(
            metrics["help_width"],
            metrics["document_width"] * 0.55,
            "Tips must stay inside one category column instead of spanning the window.",
        )

    def test_runtime_inputs_stay_compact_while_help_uses_the_wider_reading_area(self) -> None:
        audit = self.run_harness("alignment", "1080")
        timeout = next(
            entry
            for entry in audit["entries"]
            if entry["key"] == "LITELLM_MENU_REQUEST_TIMEOUT_SECONDS"
        )

        self.assertLessEqual(timeout["control"]["width"], 232.0)
        self.assertLessEqual(timeout["value_slot"]["width"], 232.0)
        self.assertLessEqual(
            timeout["unit"]["min_x"] - timeout["control"]["max_x"],
            12.0,
            "The unit belongs directly after the compact value field.",
        )
        self.assertGreater(
            timeout["help"]["width"],
            timeout["value_slot"]["width"],
            "Only the explanatory text should use the remaining responsive column width.",
        )
        self.assertLess(
            timeout["help"]["width"],
            audit["document"]["width"] * 0.55,
            "The explanatory text must stay inside its category column.",
        )

    def test_balance_refresh_setting_uses_the_same_validated_runtime_grid(self) -> None:
        audit = self.run_harness("alignment", "1080")
        refresh = next(
            entry
            for entry in audit["entries"]
            if entry["key"] == "LITELLM_MENU_BALANCE_REFRESH_MINUTES"
        )

        self.assertEqual("LITELLM_MENU_BALANCE_REFRESH_MINUTES", refresh["key"])
        self.assertLessEqual(refresh["control"]["width"], 232.0)
        self.assertGreater(refresh["help"]["width"], refresh["control"]["width"])

    def test_wide_recovery_columns_do_not_insert_blank_holes_between_settings(self) -> None:
        audit = self.run_harness("alignment", "1080")
        recovery = next(section for section in audit["sections"] if section["category"] == "Recovery")
        entries = {
            entry["key"]: entry
            for entry in audit["entries"]
            if entry["key"].startswith("LITELLM_MENU_RECOVERY")
            or entry["key"] == "LITELLM_MENU_SAME_DEPLOYMENT_RETRIES"
        }

        self.assertEqual(len(recovery["columns"]), 2)
        for column in recovery["columns"]:
            arranged = column["arranged"]
            rows = [
                view["frame"]
                for view in arranged
                if view["identifier"].startswith("RuntimeSettingsRow.")
            ]
            spacer = next(
                view["frame"]
                for view in arranged
                if view["identifier"] == "RuntimeSettingsColumnBottomSpacer"
            )

            self.assertGreater(len(rows), 0)
            self.assertAlmostEqual(rows[0]["min_y"], column["frame"]["min_y"], delta=1.0)
            for previous, current in zip(rows, rows[1:]):
                self.assertAlmostEqual(
                    current["min_y"] - previous["max_y"],
                    8.0,
                    delta=1.0,
                    msg="Only the stack's normal row spacing may appear between runtime settings.",
                )
            self.assertGreaterEqual(spacer["min_y"], rows[-1]["max_y"] + 7.0)

        for entry in entries.values():
            self.assertAlmostEqual(
                entry["input_row"]["height"],
                26.0,
                delta=1.0,
                msg=f"{entry['key']} must keep its control line compact in a wide category.",
            )

    def test_every_runtime_row_uses_the_shared_alignment_grid(self) -> None:
        def boundary(frame: dict[str, float]) -> tuple[float, float]:
            return (frame["min_x"], frame["max_x"])

        for width, expected_columns in (
            (760, 1),
            (900, 1),
            (1000, 2),
        ):
            with self.subTest(width=width):
                audit = self.run_harness("alignment", str(width))
                entries = audit["entries"]
                self.assertEqual(len(entries), len(self.payload["settings"]))
                self.assertTrue(
                    all(columns == 1 for columns in audit["category_columns"])
                    if expected_columns == 1
                    else any(columns == 2 for columns in audit["category_columns"]),
                )
                expected_columns_by_section_and_column = {}
                for entry in entries:
                    slot = (entry["section_index"], entry["column_index"])
                    expected_columns_by_section_and_column.setdefault(
                        slot,
                        {
                            name: boundary(entry[name])
                            for name in ("label", "value_slot", "action_slot", "unit")
                        },
                    )

                for entry in entries:
                    with self.subTest(width=width, key=entry["key"]):
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

                        slot = (entry["section_index"], entry["column_index"])
                        for name, expected in expected_columns_by_section_and_column[slot].items():
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
        setting_keys = {item["key"] for item in settings}
        self.assertGreaterEqual(len(settings), 47)
        self.assertIn("LITELLM_MENU_REQUEST_TIMEOUT_SECONDS", setting_keys)
        self.assertIn("LITELLM_MENU_VISION_BRIDGE_API_KEY", setting_keys)
        self.assertIn("LITELLM_CONFIG_WATCH_SETTLE_INTERVAL", setting_keys)
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
