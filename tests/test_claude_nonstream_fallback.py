from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hook_test_utils import ROOT
from young_router.core.domains.claude import ClaudeSettingsDomain

_ENV_KEY = "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK"


class ClaudeNonstreamFallbackSwitchTests(unittest.TestCase):
    def test_the_switch_writes_and_removes_the_env_key(self) -> None:
        """On writes ``1``; off removes the key and keeps every other env entry."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_BASE_URL": "http://127.0.0.1:12390",
                            "ANTHROPIC_AUTH_TOKEN": "token",
                        },
                        "model": "claude-fable-5-2",
                    }
                ),
                encoding="utf-8",
            )
            domain = ClaudeSettingsDomain(path)

            self.assertFalse(
                domain.snapshot()["clients"]["claudeCode"][
                    "nonstream_fallback_enabled"
                ]
            )

            domain.dispatch("claude.nonstream_fallback.set", {"enabled": True})
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(disk["env"][_ENV_KEY], "1")
            self.assertEqual(
                disk["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:12390"
            )
            self.assertEqual(disk["model"], "claude-fable-5-2")
            self.assertTrue(
                domain.snapshot()["clients"]["claudeCode"][
                    "nonstream_fallback_enabled"
                ]
            )

            domain.dispatch("claude.nonstream_fallback.set", {"enabled": False})
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(_ENV_KEY, disk["env"])
            self.assertEqual(
                disk["env"]["ANTHROPIC_AUTH_TOKEN"], "token"
            )
            self.assertFalse(
                domain.snapshot()["clients"]["claudeCode"][
                    "nonstream_fallback_enabled"
                ]
            )

    def test_the_switch_does_not_save_unrelated_draft_edits(self) -> None:
        """A switch is immediate; an unsaved draft stays a draft."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"env": {}, "model": "on-disk"}),
                encoding="utf-8",
            )
            domain = ClaudeSettingsDomain(path)
            domain.dispatch("patch", {"model": "unsaved-draft"})

            domain.dispatch("claude.nonstream_fallback.set", {"enabled": True})

            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(disk["model"], "on-disk")
            self.assertEqual(disk["env"][_ENV_KEY], "1")
            draft = domain.draft_state()
            self.assertEqual(draft["model"], "unsaved-draft")
            self.assertEqual(draft["env"][_ENV_KEY], "1")

    def test_the_switch_rejects_a_non_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{}", encoding="utf-8")
            domain = ClaudeSettingsDomain(path)
            with self.assertRaises(Exception):
                domain.dispatch(
                    "claude.nonstream_fallback.set", {"enabled": "yes"}
                )


class ClaudeNonstreamFallbackSwitchUiTests(unittest.TestCase):
    def test_the_claude_pane_has_the_two_way_switch(self) -> None:
        ui = (
            ROOT / "rn/packages/shared/src/ui/YoungRouterApp.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('translate("claude.nonstreamFallback")', ui)
        self.assertIn('translate("claude.nonstreamFallbackHint")', ui)
        self.assertIn(
            'dispatch("claude.nonstream_fallback.set", { enabled }, "claude")',
            ui,
        )
        self.assertIn(
            "value={booleanValue(claudeCode.nonstream_fallback_enabled)}",
            ui,
        )
        en = (
            ROOT / "rn/packages/shared/src/i18n/en.ts"
        ).read_text(encoding="utf-8")
        zh = (
            ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '"claude.nonstreamFallback": "Disable non-streaming fallback"', en
        )
        self.assertIn('"claude.nonstreamFallback": "禁用非流式回退"', zh)
        types = (
            ROOT / "rn/packages/shared/src/i18n/types.ts"
        ).read_text(encoding="utf-8")
        self.assertIn('| "claude.nonstreamFallback"', types)


if __name__ == "__main__":
    unittest.main()
