from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hook_test_utils import ROOT
from young_router.core.domains.claude import ClaudeSettingsDomain

_KEY = "disableAutoMode"


class ClaudeAutoModeSwitchTests(unittest.TestCase):
    def test_the_switch_is_on_until_it_writes_the_disable_literal(self) -> None:
        """On removes the key; off writes the client's only accepted value."""

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

            self.assertTrue(
                domain.snapshot()["clients"]["claudeCode"]["auto_mode_enabled"]
            )

            domain.dispatch("claude.auto_mode.set", {"enabled": False})
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(disk[_KEY], "disable")
            self.assertEqual(
                disk["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:12390"
            )
            self.assertEqual(disk["model"], "claude-fable-5-2")
            self.assertFalse(
                domain.snapshot()["clients"]["claudeCode"]["auto_mode_enabled"]
            )

            domain.dispatch("claude.auto_mode.set", {"enabled": True})
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(_KEY, disk)
            self.assertEqual(disk["env"]["ANTHROPIC_AUTH_TOKEN"], "token")
            self.assertTrue(
                domain.snapshot()["clients"]["claudeCode"]["auto_mode_enabled"]
            )

    def test_a_nested_disable_literal_reads_as_off_and_is_cleaned(self) -> None:
        """The client honours both levels, so enabling must clear both."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"permissions": {_KEY: "disable", "allow": ["Bash"]}}),
                encoding="utf-8",
            )
            domain = ClaudeSettingsDomain(path)
            self.assertFalse(
                domain.snapshot()["clients"]["claudeCode"]["auto_mode_enabled"]
            )

            domain.dispatch("claude.auto_mode.set", {"enabled": True})
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(_KEY, disk["permissions"])
            self.assertEqual(disk["permissions"]["allow"], ["Bash"])

    def test_the_switch_does_not_save_unrelated_draft_edits(self) -> None:
        """A switch is immediate; an unsaved draft stays a draft."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"env": {}, "model": "on-disk"}), encoding="utf-8"
            )
            domain = ClaudeSettingsDomain(path)
            domain.dispatch("patch", {"model": "unsaved-draft"})

            domain.dispatch("claude.auto_mode.set", {"enabled": False})

            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(disk["model"], "on-disk")
            self.assertEqual(disk[_KEY], "disable")
            draft = domain.draft_state()
            self.assertEqual(draft["model"], "unsaved-draft")
            self.assertEqual(draft[_KEY], "disable")

    def test_the_switch_rejects_a_non_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{}", encoding="utf-8")
            domain = ClaudeSettingsDomain(path)
            with self.assertRaises(Exception):
                domain.dispatch("claude.auto_mode.set", {"enabled": "yes"})


class ClaudeAutoModeSwitchUiTests(unittest.TestCase):
    def test_the_claude_pane_has_the_two_way_switch(self) -> None:
        ui = (
            ROOT / "rn/packages/shared/src/ui/YoungRouterApp.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('translate("claude.autoMode")', ui)
        self.assertIn('translate("claude.autoModeHint")', ui)
        self.assertIn(
            'dispatch("claude.auto_mode.set", { enabled }, "claude")',
            ui,
        )
        self.assertIn(
            "value={booleanValue(claudeCode.auto_mode_enabled)}",
            ui,
        )
        en = (
            ROOT / "rn/packages/shared/src/i18n/en.ts"
        ).read_text(encoding="utf-8")
        zh = (
            ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts"
        ).read_text(encoding="utf-8")
        self.assertIn('"claude.autoMode": "Auto permission mode"', en)
        self.assertIn('"claude.autoMode": "自动权限模式"', zh)
        types = (
            ROOT / "rn/packages/shared/src/i18n/types.ts"
        ).read_text(encoding="utf-8")
        self.assertIn('| "claude.autoMode"', types)


if __name__ == "__main__":
    unittest.main()
