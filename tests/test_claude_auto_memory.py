from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hook_test_utils import ROOT
from young_router.core.domains.claude import ClaudeSettingsDomain

_KEY = "autoMemoryEnabled"
_ENV_KEY = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


class ClaudeAutoMemorySwitchTests(unittest.TestCase):
    def test_an_absent_key_reads_as_on_and_off_writes_false(self) -> None:
        """The client's default is on, so only an explicit false turns it off."""

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
                domain.snapshot()["clients"]["claudeCode"]["auto_memory_enabled"]
            )

            domain.dispatch("claude.auto_memory.set", {"enabled": False})
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(disk[_KEY], False)
            self.assertEqual(
                disk["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:12390"
            )
            self.assertEqual(disk["model"], "claude-fable-5-2")
            self.assertFalse(
                domain.snapshot()["clients"]["claudeCode"]["auto_memory_enabled"]
            )

            domain.dispatch("claude.auto_memory.set", {"enabled": True})
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(_KEY, disk)
            self.assertEqual(disk["env"]["ANTHROPIC_AUTH_TOKEN"], "token")
            self.assertTrue(
                domain.snapshot()["clients"]["claudeCode"]["auto_memory_enabled"]
            )

    def test_the_env_gate_reads_as_off_and_is_cleared_by_the_switch(self) -> None:
        """One gate, one answer: a document carrying the env spelling cannot
        keep the pane on while the session it starts reads nothing."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"env": {_ENV_KEY: "1", "PATH": "/usr/bin"}}),
                encoding="utf-8",
            )
            domain = ClaudeSettingsDomain(path)
            self.assertFalse(
                domain.snapshot()["clients"]["claudeCode"]["auto_memory_enabled"]
            )

            domain.dispatch("claude.auto_memory.set", {"enabled": True})
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(_ENV_KEY, disk["env"])
            self.assertEqual(disk["env"]["PATH"], "/usr/bin")
            self.assertTrue(
                domain.snapshot()["clients"]["claudeCode"]["auto_memory_enabled"]
            )

    def test_the_switch_does_not_save_unrelated_draft_edits(self) -> None:
        """A switch is immediate; an unsaved draft stays a draft."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"env": {}, "model": "on-disk"}), encoding="utf-8"
            )
            domain = ClaudeSettingsDomain(path)
            domain.dispatch("patch", {"model": "unsaved-draft"})

            domain.dispatch("claude.auto_memory.set", {"enabled": False})

            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(disk["model"], "on-disk")
            self.assertIs(disk[_KEY], False)
            draft = domain.draft_state()
            self.assertEqual(draft["model"], "unsaved-draft")
            self.assertIs(draft[_KEY], False)

    def test_the_switch_rejects_a_non_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{}", encoding="utf-8")
            domain = ClaudeSettingsDomain(path)
            with self.assertRaises(Exception):
                domain.dispatch("claude.auto_memory.set", {"enabled": "yes"})


class ClaudeAutoMemorySwitchUiTests(unittest.TestCase):
    def test_the_claude_pane_has_the_two_way_switch(self) -> None:
        ui = (
            ROOT / "rn/packages/shared/src/ui/YoungRouterApp.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('translate("claude.autoMemory")', ui)
        self.assertIn('translate("claude.autoMemoryHint")', ui)
        self.assertIn(
            'dispatch("claude.auto_memory.set", { enabled }, "claude")',
            ui,
        )
        self.assertIn(
            "value={booleanValue(claudeCode.auto_memory_enabled, true)}",
            ui,
        )
        en = (
            ROOT / "rn/packages/shared/src/i18n/en.ts"
        ).read_text(encoding="utf-8")
        zh = (
            ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts"
        ).read_text(encoding="utf-8")
        self.assertIn('"claude.autoMemory": "Auto memory"', en)
        self.assertIn('"claude.autoMemory": "自动记忆"', zh)
        types = (
            ROOT / "rn/packages/shared/src/i18n/types.ts"
        ).read_text(encoding="utf-8")
        self.assertIn('"claude.autoMemory"', types)
        self.assertIn('"claude.autoMemoryHint"', types)


if __name__ == "__main__":
    unittest.main()
