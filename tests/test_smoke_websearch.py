from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_smoke_module():
    path = ROOT / "scripts" / "smoke_websearch.py"
    spec = importlib.util.spec_from_file_location("litellm_menu_smoke_websearch", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SmokeWebSearchTests(unittest.TestCase):
    def test_trace_cursor_survives_rotation_and_deduplicates_files(self) -> None:
        smoke = load_smoke_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "menu-server.log"
            backup = Path(f"{log}.1")
            old_event = {"event": "old", "request_id": "old-request"}
            new_event = {"event": "selected_deployment", "request_id": "new-request"}
            prefix = "litellm_route_trace "
            old_line = prefix + json.dumps(old_event) + "\n"
            new_line = prefix + json.dumps(new_event) + "\n"
            log.write_text(old_line, encoding="utf-8")

            cursor = smoke.route_trace_cursor(str(log))
            backup.write_text(old_line + new_line, encoding="utf-8")
            log.write_text(old_line + new_line, encoding="utf-8")

            self.assertEqual(
                smoke.read_route_trace_events_since(str(log), cursor),
                [new_event],
            )

    def test_collect_urls_normalizes_provider_markdown_citation(self) -> None:
        smoke = load_smoke_module()
        self.assertEqual(
            smoke.collect_urls(
                "https://example.test/page[[1]](https://example.test/page)"
            ),
            ["https://example.test/page"],
        )
        self.assertEqual(
            smoke.collect_urls(
                "https://example.test/page.[[1]](https://example.test/page)"
            ),
            ["https://example.test/page"],
        )
        self.assertEqual(
            smoke.collect_urls("https://example.test/wiki/(topic)"),
            ["https://example.test/wiki/(topic)"],
        )

    def test_trace_selection_prefers_exact_model_group_during_concurrency(self) -> None:
        smoke = load_smoke_module()
        events = [
            {
                "event": "generic_fallback_helper_start",
                "request_id": "target",
                "model_group": "synthetic-route-id",
            },
            {
                "event": "selected_deployment",
                "request_id": "target",
                "model_group": "synthetic-route-id",
                "deployment": {"id": "synthetic-route-id"},
            },
            {
                "event": "external_web_search_bridge_start",
                "request_id": "concurrent",
                "model_group": "other-model",
                "request_preview": {
                    "preview": "synthetic-route-id synthetic query"
                },
            },
        ]

        selected, match = smoke.select_route_trace_events(
            events,
            request_id="client-id-not-forwarded",
            query="synthetic query",
            model="synthetic-route-id",
        )

        self.assertEqual({event["request_id"] for event in selected}, {"target"})
        self.assertEqual(match["matched_by"], "exact_model_group_score")


if __name__ == "__main__":
    unittest.main()
