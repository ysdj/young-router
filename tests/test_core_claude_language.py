from __future__ import annotations

import json
import os
import pathlib
import stat
import tempfile
import unittest

from litellm_menu.core import ConfirmationNeeded, CoreStore
from litellm_menu.core.domains.claude import (
    ClaudeSettingsDomain,
    ClaudeSettingsError,
    ConfirmationRequired,
)
from litellm_menu.core.domains.language import (
    LANGUAGE_OPTIONS,
    LanguageSettingsDomain,
    LanguageSettingsError,
    create_translator,
    resolve_language,
)


class ClaudeSettingsDomainTests(unittest.TestCase):
    def test_core_registers_both_domains_and_keeps_apply_confirmation_at_the_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            core = CoreStore.with_default_domains(
                claude_settings_path=root / "claude" / "settings.json",
                language_path=root / "language.json",
            )
            self.assertIn("claude", core.domains)
            self.assertIn("language", core.domains)
            core.dispatch(
                {
                    "type": "patch",
                    "domain": "claude",
                    "payload": {"permissions": {"defaultMode": "bypassPermissions"}},
                }
            )
            core.stage_secret("claude", "deployment_token", None, "synthetic-core-token", revision=core.revision)
            self.assertNotIn("synthetic-core-token", json.dumps(core.snapshot()))
            self.assertNotIn(str(root), json.dumps(core.snapshot()))
            with self.assertRaises(ConfirmationNeeded):
                core.apply("claude", revision=core.revision)
            core.apply("claude", revision=core.revision, confirmation="accepted")

    def test_deployment_apply_writes_only_public_connection_fields_and_redacts_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / ".claude" / "settings.json"
            original = {
                "env": {
                    "KEEP_THIS": "value",
                    "OLD_MODEL": "do-not-rewrite",
                },
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "check"}]}]},
                "statusLine": {"type": "command", "command": "status"},
                "futureField": {"keep": True},
            }
            path.parent.mkdir()
            path.write_text(json.dumps(original), encoding="utf-8")
            domain = ClaudeSettingsDomain(path)
            domain.dispatch(
                "patch",
                {"alwaysThinkingEnabled": True},
            )
            domain.dispatch(
                "select_deployment",
                {
                    "model": "public-chat",
                    "base_url": "https://gateway.example.test/v1",
                    "token": "synthetic-token",
                },
            )

            snapshot_text = json.dumps(domain.snapshot())
            self.assertNotIn("synthetic-token", snapshot_text)
            self.assertNotIn(str(root), snapshot_text)
            self.assertEqual(
                "https://gateway.example.test/v1",
                domain.snapshot()["settings"]["gateway_url"],
            )
            result = domain.apply({"confirm_risks": []})

            self.assertTrue(result["applied"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("public-chat", saved["model"])
            self.assertEqual("public-chat", saved["env"]["ANTHROPIC_MODEL"])
            self.assertEqual("https://gateway.example.test/v1", saved["env"]["ANTHROPIC_BASE_URL"])
            self.assertEqual("synthetic-token", saved["env"]["ANTHROPIC_AUTH_TOKEN"])
            self.assertEqual("value", saved["env"]["KEEP_THIS"])
            self.assertEqual("do-not-rewrite", saved["env"]["OLD_MODEL"])
            self.assertTrue(saved["alwaysThinkingEnabled"])
            self.assertEqual(original["hooks"], saved["hooks"])
            self.assertEqual(original["statusLine"], saved["statusLine"])
            self.assertEqual({"keep": True}, saved["futureField"])
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_snapshot_omits_gateway_urls_with_embedded_query_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / ".claude" / "settings.json"
            path.parent.mkdir()
            path.write_text(
                json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example.test/v1?token=synthetic-token"}}),
                encoding="utf-8",
            )

            domain = ClaudeSettingsDomain(path)

            snapshot_text = json.dumps(domain.snapshot())
            self.assertIsNone(domain.snapshot()["settings"]["gateway_url"])
            self.assertNotIn("synthetic-token", snapshot_text)

    def test_snapshot_redacts_permission_rules_and_excluded_commands_without_changing_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            original = {
                "permissions": {
                    "allow": ["Read(/private/synthetic-project/**)"],
                    "ask": ["Bash(deploy --token=synthetic-permission-token)"],
                    "deny": ["Read(/private/synthetic-secret/**)"],
                },
                "sandbox": {
                    "excludedCommands": ["/private/synthetic-tool --credential=synthetic-command-token"],
                },
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            domain = ClaudeSettingsDomain(path)

            snapshot = domain.snapshot()["settings"]
            snapshot_text = json.dumps(snapshot)
            for private_value in (
                "/private/synthetic-project",
                "synthetic-permission-token",
                "/private/synthetic-secret",
                "/private/synthetic-tool",
                "synthetic-command-token",
            ):
                self.assertNotIn(private_value, snapshot_text)
            self.assertEqual(["configured"], snapshot["permissions"]["allow"])
            self.assertEqual(["configured"], snapshot["permissions"]["ask"])
            self.assertEqual(["configured"], snapshot["permissions"]["deny"])
            self.assertEqual(["configured"], snapshot["sandbox"]["excludedCommands"])

            # The trusted raw-editor path retains the real document and an
            # unrelated structured edit must not flatten the protected lists.
            self.assertIn("synthetic-command-token", domain.raw_text(include_sensitive=True))
            domain.dispatch("patch", {"verbose": True})
            domain.apply()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(original["permissions"], saved["permissions"])
            self.assertEqual(original["sandbox"]["excludedCommands"], saved["sandbox"]["excludedCommands"])

    def test_partial_deployment_patch_stages_each_public_field_and_rejects_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            domain = ClaudeSettingsDomain(path)

            domain.dispatch("patch_deployment", {"model": " public-chat "})
            self.assertEqual("public-chat", domain.snapshot()["settings"]["model"])
            self.assertFalse(domain.snapshot()["settings"]["gateway_configured"])
            domain.dispatch("patch_deployment", {"base_url": "https://gateway.example.test/v1"})
            self.assertEqual("https://gateway.example.test/v1", domain.snapshot()["settings"]["gateway_url"])

            with self.assertRaisesRegex(ClaudeSettingsError, "Unknown Claude deployment field"):
                domain.dispatch("patch_deployment", {"token": "synthetic-token"})
            with self.assertRaisesRegex(ClaudeSettingsError, "http or https"):
                domain.dispatch("patch_deployment", {"base_url": "not-a-url"})

            domain.dispatch("patch_deployment", {"model": ""})
            domain.dispatch("patch_deployment", {"base_url": ""})
            domain.apply()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("model", saved)
            self.assertNotIn("ANTHROPIC_MODEL", saved.get("env", {}))
            self.assertNotIn("ANTHROPIC_BASE_URL", saved.get("env", {}))

    def test_risky_permissions_are_denied_until_each_confirmation_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ClaudeSettingsDomain(pathlib.Path(directory) / "settings.json")
            domain.dispatch(
                "patch",
                {
                    "permissions": {"defaultMode": "bypassPermissions"},
                    "sandbox": {
                        "enabled": False,
                        "filesystem": {"allowWrite": ["*"]},
                        "network": {"allowedDomains": ["*"]},
                    },
                },
            )
            with self.assertRaises(ConfirmationRequired) as context:
                domain.apply()
            self.assertEqual(
                {
                    "bypass_permissions",
                    "sandbox_disabled",
                    "filesystem_scope_broadened",
                    "network_scope_broadened",
                },
                set(context.exception.codes),
            )
            self.assertNotIn("settings.json", json.dumps(domain.snapshot()))
            with self.assertRaises(ConfirmationRequired):
                domain.apply({"confirm_risks": ["bypass_permissions"]})
            domain.apply(
                {
                    "confirm_risks": [
                        "bypass_permissions",
                        "sandbox_disabled",
                        "filesystem_scope_broadened",
                        "network_scope_broadened",
                    ]
                }
            )

            # The shared IPC apply envelope carries one explicit accepted
            # confirmation token after the native confirmation sheet; the
            # adapter accepts that token but never treats an omitted token as
            # consent.
            domain.dispatch("patch", {"sandbox": {"network": {"allowedDomains": ["0.0.0.0/0"]}}})
            with self.assertRaises(ConfirmationRequired):
                domain.apply()
            domain.apply({"confirmation": "accepted"})

    def test_filesystem_isolation_disable_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ClaudeSettingsDomain(pathlib.Path(directory) / "settings.json")
            domain.dispatch("patch", {"sandbox": {"filesystem": {"disabled": True}}})

            self.assertIn(
                "sandbox_disabled",
                domain.snapshot()["settings"]["risk_confirmations"],
            )
            with self.assertRaises(ConfirmationRequired):
                domain.apply()
            domain.apply({"confirm_risks": ["sandbox_disabled"]})

    def test_canonical_structured_settings_round_trip_and_unknown_json_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "settings.json"
            original = {
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "done"}]}]},
                "statusLine": {"type": "command", "command": "status"},
                "future": {"nested": [1, {"keep": True}]},
                "permissions": {"futurePermissionControl": {"keep": True}},
                "sandbox": {
                    "futureSandboxControl": {"keep": True},
                    "filesystem": {"futureFilesystemControl": 9},
                    "network": {"futureNetworkControl": 7},
                },
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            domain = ClaudeSettingsDomain(path)
            canonical = {
                "model": "public-model",
                "advisorModel": "sonnet",
                "agent": "reviewer",
                "fallbackModel": ["fallback-one", "fallback-two"],
                "availableModels": ["public-model", "fallback-one"],
                "effortLevel": "future-effort-value",
                "alwaysThinkingEnabled": True,
                "showThinkingSummaries": False,
                "fastMode": True,
                "fastModePerSessionOptIn": True,
                "autoCompactEnabled": False,
                "autoMemoryEnabled": False,
                "fileCheckpointingEnabled": True,
                "attribution": {
                    "commit": "Generated with Claude\n\nCo-Authored-By: Claude <noreply@example.test>",
                    "pr": "Generated with Claude",
                    "sessionUrl": False,
                },
                "autoMode": {
                    "classifyAllShell": True,
                    "environment": ["$defaults", "Internal staging is trusted"],
                    "allow": ["$defaults", "Routine staging deploys are allowed"],
                    "soft_deny": ["$defaults", "Deleting a non-ephemeral bucket"],
                    "hard_deny": ["$defaults", "Export production customer data"],
                },
                "autoUpdatesChannel": "stable",
                "editorMode": "vim",
                "theme": "custom:neutral",
                "verbose": True,
                "viewMode": "focus",
                "tui": "fullscreen",
                "teammateMode": "iterm2",
                "preferredNotifChannel": "terminal_bell",
                "askUserQuestionTimeout": "5m",
                "language": "chinese",
                "outputStyle": "Explanatory",
                "defaultShell": "powershell",
                "workflowSizeGuideline": "small",
                "cleanupPeriodDays": 30,
                "respectGitignore": False,
                "includeGitInstructions": False,
                "enableAllProjectMcpServers": False,
                "enabledMcpjsonServers": ["memory"],
                "disabledMcpjsonServers": ["filesystem"],
                "agentPushNotifEnabled": True,
                "inputNeededNotifEnabled": True,
                "remoteControlAtStartup": False,
                "awaySummaryEnabled": False,
                "spinnerTipsEnabled": False,
                "terminalProgressBarEnabled": False,
                "prefersReducedMotion": True,
                "axScreenReader": True,
                "syntaxHighlightingDisabled": True,
                "autoScrollEnabled": False,
                "wheelScrollAccelerationEnabled": False,
                "showTurnDuration": False,
                "enableArtifact": True,
                "disableWorkflows": False,
                "workflowKeywordTriggerEnabled": True,
                "emojiCompletionEnabled": False,
                "respondToBashCommands": False,
                "showClearContextOnPlanAccept": True,
                "switchModelsOnFlag": False,
                "useAutoModeDuringPlan": False,
                "vimInsertModeRemaps": {"jj": "<Esc>"},
                "voice": {"enabled": True, "mode": "hold", "autoSubmit": True},
                "permissions": {
                    "defaultMode": "futurePermissionMode",
                    "allow": ["Read"],
                    "ask": ["Bash(git push *)"],
                    "deny": ["Read(./secret/**)"],
                    "additionalDirectories": ["../docs"],
                },
                "sandbox": {
                    "enabled": True,
                    "failIfUnavailable": True,
                    "autoAllowBashIfSandboxed": False,
                    "allowUnsandboxedCommands": False,
                    "enableWeakerNestedSandbox": True,
                    "enableWeakerNetworkIsolation": True,
                    "allowAppleEvents": True,
                    "excludedCommands": ["docker *"],
                    "filesystem": {
                        "allowWrite": ["/tmp/build"],
                        "denyWrite": ["/etc"],
                        "allowRead": ["."],
                        "denyRead": ["~/.aws"],
                        "disabled": False,
                    },
                    "network": {
                        "allowedDomains": ["example.test"],
                        "deniedDomains": ["private.example.test"],
                        "allowUnixSockets": ["~/.ssh/agent.sock"],
                        "allowAllUnixSockets": False,
                        "allowLocalBinding": True,
                        "strictAllowlist": True,
                        "httpProxyPort": 8080,
                        "socksProxyPort": 1080,
                        "allowMachLookup": ["com.example.service"],
                    },
                },
            }
            domain.dispatch("patch", canonical)
            domain.stage_secret("auto_memory_directory", None, "/private/synthetic-claude-memory")

            snapshot = domain.snapshot()["settings"]
            snapshot_text = json.dumps(snapshot)
            self.assertTrue(snapshot["autoMemoryDirectoryConfigured"])
            self.assertNotIn("/private/synthetic-claude-memory", snapshot_text)
            self.assertEqual(canonical["attribution"], snapshot["attribution"])
            self.assertEqual(canonical["autoMode"], snapshot["autoMode"])
            self.assertEqual(canonical["autoUpdatesChannel"], snapshot["autoUpdatesChannel"])
            self.assertEqual(canonical["vimInsertModeRemaps"], snapshot["vimInsertModeRemaps"])
            self.assertEqual(canonical["voice"], snapshot["voice"])
            self.assertEqual("futurePermissionMode", snapshot["permissions"]["defaultMode"])
            self.assertEqual(["configured"], snapshot["permissions"]["additionalDirectories"])
            self.assertEqual(["configured"], snapshot["sandbox"]["filesystem"]["allowWrite"])
            self.assertEqual(["configured"], snapshot["sandbox"]["network"]["allowedDomains"])
            self.assertNotIn("permissions_mode", snapshot)
            self.assertNotIn("network", snapshot)

            domain.apply()
            saved = json.loads(path.read_text(encoding="utf-8"))
            for key, value in canonical.items():
                if key not in {"permissions", "sandbox"}:
                    self.assertEqual(value, saved[key])
            self.assertEqual("/private/synthetic-claude-memory", saved["autoMemoryDirectory"])
            for key, value in canonical["permissions"].items():
                self.assertEqual(value, saved["permissions"][key])
            for key, value in canonical["sandbox"].items():
                if key not in {"filesystem", "network"}:
                    self.assertEqual(value, saved["sandbox"][key])
            for key, value in canonical["sandbox"]["filesystem"].items():
                self.assertEqual(value, saved["sandbox"]["filesystem"][key])
            for key, value in canonical["sandbox"]["network"].items():
                self.assertEqual(value, saved["sandbox"]["network"][key])
            self.assertEqual(original["hooks"], saved["hooks"])
            self.assertEqual(original["statusLine"], saved["statusLine"])
            self.assertEqual(original["future"], saved["future"])
            self.assertEqual(
                original["permissions"]["futurePermissionControl"],
                saved["permissions"]["futurePermissionControl"],
            )
            self.assertEqual(
                original["sandbox"]["futureSandboxControl"],
                saved["sandbox"]["futureSandboxControl"],
            )
            self.assertEqual(9, saved["sandbox"]["filesystem"]["futureFilesystemControl"])
            self.assertEqual(7, saved["sandbox"]["network"]["futureNetworkControl"])
            self.assertNotIn("permissions_mode", saved)
            self.assertNotIn("network", saved)
            self.assertNotIn("network_access", saved)
            self.assertNotIn("desktop_profile", saved)

    def test_new_structured_claude_fields_support_partial_patch_and_validate_official_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "attribution": {"commit": "initial", "future": "keep"},
                        "autoMode": {"soft_deny": ["$defaults"], "future": True},
                        "vimInsertModeRemaps": {"jk": "<Esc>"},
                        "voice": {"enabled": True, "mode": "tap", "future": "keep"},
                    }
                ),
                encoding="utf-8",
            )
            domain = ClaudeSettingsDomain(path)
            domain.dispatch(
                "patch",
                {
                    "attribution": {"pr": "Generated by tests", "sessionUrl": False},
                    "autoMode": {"classifyAllShell": True, "environment": ["$defaults"]},
                    "autoUpdatesChannel": "latest",
                    "vimInsertModeRemaps": {"jj": "<Esc>"},
                    "voice": {"autoSubmit": True, "mode": "hold"},
                },
            )
            domain.stage_secret("auto_memory_directory", None, "~/synthetic-memory")

            snapshot = domain.snapshot()["settings"]
            self.assertTrue(snapshot["autoMemoryDirectoryConfigured"])
            self.assertNotIn("~/synthetic-memory", json.dumps(snapshot))
            self.assertEqual({"commit": "initial", "pr": "Generated by tests", "sessionUrl": False}, snapshot["attribution"])
            self.assertEqual(
                {"classifyAllShell": True, "environment": ["$defaults"], "soft_deny": ["$defaults"]},
                snapshot["autoMode"],
            )
            self.assertEqual({"jk": "<Esc>", "jj": "<Esc>"}, snapshot["vimInsertModeRemaps"])
            self.assertEqual({"enabled": True, "mode": "hold", "autoSubmit": True}, snapshot["voice"])

            domain.apply()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("~/synthetic-memory", saved["autoMemoryDirectory"])
            self.assertEqual("initial", saved["attribution"]["commit"])
            self.assertEqual("Generated by tests", saved["attribution"]["pr"])
            self.assertFalse(saved["attribution"]["sessionUrl"])
            self.assertEqual("keep", saved["attribution"]["future"])
            self.assertTrue(saved["autoMode"]["classifyAllShell"])
            self.assertEqual(["$defaults"], saved["autoMode"]["environment"])
            self.assertEqual(["$defaults"], saved["autoMode"]["soft_deny"])
            self.assertTrue(saved["autoMode"]["future"])
            self.assertEqual({"jk": "<Esc>", "jj": "<Esc>"}, saved["vimInsertModeRemaps"])
            self.assertTrue(saved["voice"]["enabled"])
            self.assertEqual("hold", saved["voice"]["mode"])
            self.assertTrue(saved["voice"]["autoSubmit"])
            self.assertEqual("keep", saved["voice"]["future"])

            for payload in (
                {"autoMemoryDirectory": "~/must-use-native-input"},
                {"attribution": {"sessionUrl": "false"}},
                {"attribution": {"unknown": True}},
                {"autoMode": {"classifyAllShell": "true"}},
                {"autoMode": {"environment": "not-a-list"}},
                {"autoMode": {"unknown": True}},
                {"autoUpdatesChannel": "preview"},
                {"vimInsertModeRemaps": {"j": "<Esc>"}},
                {"vimInsertModeRemaps": {"jj": "escape"}},
                {"voice": {"enabled": "true"}},
                {"voice": {"mode": "always"}},
                {"voice": {"unknown": True}},
            ):
                with self.subTest(payload=payload), self.assertRaises(ClaudeSettingsError):
                    domain.dispatch("patch", payload)

    def test_user_safe_claude_controls_round_trip_without_flattening_raw_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            original = {
                "permissions": {
                    "disableBypassPermissionsMode": "disable",
                    "futurePermissionControl": {"keep": True},
                },
                "skillOverrides": {"existing-skill": "on"},
                "spinnerTipsOverride": {"tips": ["Existing tip"], "future": {"keep": True}},
                "spinnerVerbs": {"verbs": ["Existing"], "mode": "append", "future": True},
                "worktree": {"baseRef": "fresh", "symlinkDirectories": ["node_modules"]},
                "futureTopLevel": {"keep": True},
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            domain = ClaudeSettingsDomain(path)
            patch = {
                "disableAutoMode": "disable",
                "disableBundledSkills": True,
                "disableClaudeAiConnectors": True,
                "disableRemoteControl": True,
                "disableDeepLinkRegistration": "disable",
                "disableSkillShellExecution": True,
                "disableAllHooks": True,
                "autoConnectIde": True,
                "autoInstallIdeExtension": False,
                "externalEditorContext": True,
                "permissionExplainerEnabled": False,
                "disableAgentView": True,
                "disableArtifact": True,
                "skipWebFetchPreflight": True,
                "diffTool": "terminal",
                "teammateDefaultModel": "sonnet",
                "feedbackSurveyRate": 0.05,
                "minimumVersion": "2.1.100",
                "skillListingBudgetFraction": 0.02,
                "skillListingMaxDescChars": 2048,
                "companyAnnouncements": ["Synthetic announcement"],
                "permissions": {"disableBypassPermissionsMode": "disable"},
                "skillOverrides": {"legacy-context": "name-only", "deploy": "off"},
                "spinnerTipsOverride": {"tips": ["Synthetic tip"], "excludeDefault": True},
                "spinnerVerbs": {"verbs": ["Pondering", "Crafting"], "mode": "replace"},
                "worktree": {"baseRef": "head", "bgIsolation": "none"},
            }
            domain.dispatch("patch", patch)

            snapshot = domain.snapshot()["settings"]
            for key in (
                "disableAutoMode",
                "disableBundledSkills",
                "disableClaudeAiConnectors",
                "disableRemoteControl",
                "disableDeepLinkRegistration",
                "disableSkillShellExecution",
                "disableAllHooks",
                "autoConnectIde",
                "autoInstallIdeExtension",
                "externalEditorContext",
                "permissionExplainerEnabled",
                "disableAgentView",
                "disableArtifact",
                "skipWebFetchPreflight",
                "diffTool",
                "teammateDefaultModel",
                "feedbackSurveyRate",
                "minimumVersion",
                "skillListingBudgetFraction",
                "skillListingMaxDescChars",
                "companyAnnouncements",
                "skillOverrides",
                "spinnerTipsOverride",
                "spinnerVerbs",
                "worktree",
            ):
                if key == "skillOverrides":
                    self.assertEqual({**original[key], **patch[key]}, snapshot[key])
                else:
                    self.assertEqual(patch[key], snapshot[key])
            self.assertEqual("disable", snapshot["permissions"]["disableBypassPermissionsMode"])
            self.assertNotIn("futureTopLevel", snapshot)
            self.assertNotIn("symlinkDirectories", snapshot["worktree"])

            domain.apply()
            saved = json.loads(path.read_text(encoding="utf-8"))
            for key, value in patch.items():
                if key not in {"permissions", "skillOverrides", "spinnerTipsOverride", "spinnerVerbs", "worktree"}:
                    self.assertEqual(value, saved[key])
            self.assertEqual("disable", saved["permissions"]["disableBypassPermissionsMode"])
            self.assertEqual(original["permissions"]["futurePermissionControl"], saved["permissions"]["futurePermissionControl"])
            self.assertEqual("on", saved["skillOverrides"]["existing-skill"])
            self.assertEqual("name-only", saved["skillOverrides"]["legacy-context"])
            self.assertEqual("off", saved["skillOverrides"]["deploy"])
            self.assertEqual({"keep": True}, saved["spinnerTipsOverride"]["future"])
            self.assertEqual(True, saved["spinnerVerbs"]["future"])
            self.assertEqual(["node_modules"], saved["worktree"]["symlinkDirectories"])
            self.assertEqual({"keep": True}, saved["futureTopLevel"])

    def test_user_safe_claude_controls_reject_invalid_or_unowned_structured_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            domain = ClaudeSettingsDomain(path)
            for payload in (
                {"disableAutoMode": "enabled"},
                {"disableDeepLinkRegistration": True},
                {"disableBundledSkills": "true"},
                {"feedbackSurveyRate": -0.01},
                {"feedbackSurveyRate": 1.01},
                {"feedbackSurveyRate": True},
                {"skillListingBudgetFraction": 0},
                {"skillListingBudgetFraction": float("nan")},
                {"skillListingMaxDescChars": 0},
                {"skillListingMaxDescChars": True},
                {"companyAnnouncements": ["ok", 7]},
                {"permissions": {"disableBypassPermissionsMode": "enabled"}},
                {"permissions": {"unknown": True}},
                {"skillOverrides": {"review": "visible"}},
                {"skillOverrides": {"bad\nname": "on"}},
                {"spinnerTipsOverride": {"tips": "not-a-list"}},
                {"spinnerTipsOverride": {"future": True}},
                {"spinnerVerbs": {"verbs": ["Thinking"], "mode": "always"}},
                {"spinnerVerbs": {"unknown": True}},
                {"worktree": {"baseRef": "remote"}},
                {"worktree": {"symlinkDirectories": ["node_modules"]}},
            ):
                with self.subTest(payload=payload), self.assertRaises(ClaudeSettingsError):
                    domain.dispatch("patch", payload)

    def test_rejected_structured_patch_is_atomic_and_valid_values_are_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text(json.dumps({"future": {"keep": True}}), encoding="utf-8")
            domain = ClaudeSettingsDomain(path)
            before = domain.raw_text(include_sensitive=True)
            revision = domain.snapshot()["revision"]

            with self.assertRaises(ClaudeSettingsError):
                domain.dispatch("patch", {"cleanupPeriodDays": 0})

            self.assertEqual(before, domain.raw_text(include_sensitive=True))
            self.assertEqual(revision, domain.snapshot()["revision"])
            domain.dispatch(
                "patch",
                {
                    "autoUpdatesChannel": " latest ",
                    "companyAnnouncements": ["  Public announcement  "],
                    "skillOverrides": {"  review  ": "on"},
                },
            )
            snapshot = domain.snapshot()["settings"]
            self.assertEqual("latest", snapshot["autoUpdatesChannel"])
            self.assertEqual(["Public announcement"], snapshot["companyAnnouncements"])
            self.assertEqual({"review": "on"}, snapshot["skillOverrides"])

            domain.apply()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("latest", saved["autoUpdatesChannel"])
            self.assertEqual(["Public announcement"], saved["companyAnnouncements"])
            self.assertEqual({"review": "on"}, saved["skillOverrides"])

    def test_structured_patch_rejects_noncanonical_aliases_but_raw_json_preserves_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            domain = ClaudeSettingsDomain(path)
            for alias in ("permissions_mode", "network", "network_access", "desktop_profile"):
                with self.subTest(alias=alias), self.assertRaisesRegex(ClaudeSettingsError, "Unknown"):
                    domain.dispatch("patch", {alias: {}})
            for payload in (
                {"permissions": {"mode": "bypassPermissions"}},
                {"permissions": {"default_mode": "bypassPermissions"}},
                {"permissions": {"network_access": True}},
                {"sandbox": {"writable_paths": ["*"]}},
                {"sandbox": {"filesystem": {"allowed_paths": ["*"]}}},
                {"sandbox": {"network": {"allowed_domains": ["*"]}}},
                {"sandbox": {"filesystem": {"allowManagedReadPathsOnly": True}}},
                {"sandbox": {"network": {"allowManagedDomainsOnly": True}}},
            ):
                with self.subTest(payload=payload), self.assertRaisesRegex(ClaudeSettingsError, "Unknown"):
                    domain.dispatch("patch", payload)

            raw = {
                "permissions_mode": "legacy",
                "network": {"enabled": True},
                "network_access": {"future": True},
                "desktop_profile": {"future": 7},
                "hooks": {"future": True},
            }
            domain.dispatch("set_raw", {"raw_json": json.dumps(raw)})
            domain.dispatch("patch", {"permissions": {"defaultMode": "default"}})
            domain.apply()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["network"], saved["network"])
            self.assertEqual(raw["network_access"], saved["network_access"])
            self.assertEqual(raw["desktop_profile"], saved["desktop_profile"])
            self.assertEqual(raw["hooks"], saved["hooks"])
            self.assertEqual("default", saved["permissions"]["defaultMode"])

    def test_validation_type_checks_canonical_controls_without_pinching_future_enums(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ClaudeSettingsDomain(pathlib.Path(directory) / "settings.json")
            domain.dispatch(
                "patch",
                {
                    "effortLevel": "future-effort",
                    "editorMode": "future-editor",
                    "permissions": {"defaultMode": "future-permission-mode"},
                },
            )
            for payload in (
                {"fastMode": "true"},
                {"cleanupPeriodDays": 0},
                {"availableModels": ["ok", 7]},
                {"permissions": {"allow": "Read"}},
                {"sandbox": {"filesystem": {"disabled": "false"}}},
                {"sandbox": {"network": {"httpProxyPort": "8080"}}},
            ):
                with (
                    self.subTest(payload=payload),
                    tempfile.TemporaryDirectory(dir=directory) as case_directory,
                    self.assertRaises(ClaudeSettingsError),
                ):
                    fresh = ClaudeSettingsDomain(pathlib.Path(case_directory) / "settings.json")
                    fresh.dispatch("patch", payload)

    def test_fallback_models_are_limited_to_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ClaudeSettingsDomain(pathlib.Path(directory) / "settings.json")
            domain.dispatch("patch", {"fallbackModel": ["one", "two", "three"]})
            with self.assertRaisesRegex(ClaudeSettingsError, "at most 3 fallback models"):
                domain.dispatch("patch", {"fallbackModel": ["one", "two", "three", "four"]})

    def test_invalid_json_and_symlinks_are_safe_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "settings.json"
            secret = "synthetic-invalid-json"
            path.write_text('{"token": "' + secret, encoding="utf-8")
            with self.assertRaises(ClaudeSettingsError) as context:
                ClaudeSettingsDomain(path)
            self.assertNotIn(secret, str(context.exception))

            target = root / "real.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaises(ClaudeSettingsError):
                ClaudeSettingsDomain(link)

    def test_apply_refuses_an_external_disk_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text('{"model":"initial"}\n', encoding="utf-8")
            domain = ClaudeSettingsDomain(path)
            domain.dispatch("patch", {"model": "draft"})
            external = '{"model":"external"}\n'
            path.write_text(external, encoding="utf-8")

            with self.assertRaisesRegex(ClaudeSettingsError, "changed on disk"):
                domain.apply()

            self.assertEqual(external, path.read_text(encoding="utf-8"))

    def test_external_disk_state_and_rebase_keep_the_staged_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text('{"model":"initial","hooks":{"keep":true}}\n', encoding="utf-8")
            domain = ClaudeSettingsDomain(path)
            domain.dispatch("patch", {"model": "draft"})
            draft_before = domain.raw_text(include_sensitive=True)

            self.assertEqual({"changed": False, "exists": True}, domain.external_disk_state())
            path.write_text('{"model":"external","hooks":{"external":true}}\n', encoding="utf-8")
            self.assertEqual({"changed": True, "exists": True}, domain.external_disk_state())

            previous_revision = domain.snapshot()["revision"]
            domain.rebase_external_disk()
            self.assertEqual(draft_before, domain.raw_text(include_sensitive=True))
            self.assertGreater(domain.snapshot()["revision"], previous_revision)
            self.assertEqual({"changed": False, "exists": True}, domain.external_disk_state())
            domain.apply()
            self.assertEqual("draft", json.loads(path.read_text(encoding="utf-8"))["model"])

    def test_rebase_external_disk_does_not_change_the_loaded_file_exists_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            domain = ClaudeSettingsDomain(path)
            self.assertFalse(domain.snapshot()["settings"]["file_exists"])

            path.write_text('{"model":"external"}\n', encoding="utf-8")
            domain.rebase_external_disk()
            self.assertFalse(domain.snapshot()["settings"]["file_exists"])
            self.assertEqual({"changed": False, "exists": True}, domain.external_disk_state())

    def test_first_apply_refuses_a_file_created_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            domain = ClaudeSettingsDomain(path)
            domain.dispatch("patch", {"model": "draft"})
            external = '{"model":"external"}\n'
            path.write_text(external, encoding="utf-8")

            with self.assertRaisesRegex(ClaudeSettingsError, "changed on disk"):
                domain.apply()

            self.assertEqual(external, path.read_text(encoding="utf-8"))


class LanguageSettingsDomainTests(unittest.TestCase):
    def test_language_file_environment_override_keeps_preview_state_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "preview-language.json"
            previous = os.environ.get("LITELLM_MENU_LANGUAGE_FILE")
            os.environ["LITELLM_MENU_LANGUAGE_FILE"] = str(path)
            try:
                domain = LanguageSettingsDomain()
                domain.dispatch("set_language", {"language": "zh-Hans"})
                domain.apply()
            finally:
                if previous is None:
                    os.environ.pop("LITELLM_MENU_LANGUAGE_FILE", None)
                else:
                    os.environ["LITELLM_MENU_LANGUAGE_FILE"] = previous
            self.assertEqual({"language": "zh-Hans"}, json.loads(path.read_text(encoding="utf-8")))

    def test_malformed_optional_files_keep_routes_registered_and_reloadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            claude = root / "claude.json"
            language = root / "language.json"
            claude.write_text("{", encoding="utf-8")
            language.write_text("{", encoding="utf-8")
            core = CoreStore.with_default_domains(
                metadata_path=root / "core.json",
                config_path=root / "config.yaml",
                runtime_settings_path=root / "runtime.env",
                webdav_settings_path=root / "webdav.json",
                webdav_enabled_path=root / "webdav.enabled",
                claude_settings_path=claude,
                language_path=language,
                runtime_root=root,
            )

            unavailable = core.snapshot()
            self.assertIn("claude", unavailable["domains"])
            self.assertIn("language", unavailable["domains"])
            self.assertFalse(unavailable["domains"]["claude"]["available"])
            self.assertFalse(unavailable["domains"]["language"]["available"])

            claude.write_text("{}\n", encoding="utf-8")
            language.write_text('{"language":"en"}\n', encoding="utf-8")
            core.reload("claude")
            core.reload("language")
            repaired = core.snapshot()
            self.assertNotEqual(False, repaired["domains"]["claude"].get("available"))
            self.assertEqual("en", repaired["language"])

    def test_system_resolution_and_user_override(self) -> None:
        self.assertEqual("zh-Hans", resolve_language("system", "zh_CN"))
        self.assertEqual("zh-Hans", resolve_language("system", "zh-TW"))
        self.assertEqual("en", resolve_language("system", "ja-JP"))
        self.assertEqual("en", resolve_language("en", "zh-CN"))
        self.assertEqual("zh-Hans", resolve_language("zh-Hans", "en-US"))

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "language.json"
            domain = LanguageSettingsDomain(path, system_locale="zh-CN")
            self.assertEqual("zh-Hans", domain.snapshot()["resolved"])
            self.assertEqual(list(LANGUAGE_OPTIONS), domain.snapshot()["options"])
            domain.dispatch("set_language", {"language": "en"})
            self.assertEqual("en", domain.snapshot()["resolved"])
            domain.apply()
            reloaded = LanguageSettingsDomain(path, system_locale="zh-CN")
            self.assertEqual("en", reloaded.snapshot()["choice"])
            self.assertEqual("en", reloaded.snapshot()["resolved"])

    def test_invalid_choice_is_rejected_and_messages_do_not_translate_values(self) -> None:
        with self.assertRaises(LanguageSettingsError):
            resolve_language("fr")
        with tempfile.TemporaryDirectory() as directory:
            domain = LanguageSettingsDomain(pathlib.Path(directory) / "language.json")
            with self.assertRaises(LanguageSettingsError):
                domain.dispatch("set", {"language": "fr"})
        translator = create_translator("zh-Hans")
        self.assertEqual("Claude 设置", translator("menu.claude"))
        self.assertEqual("Codex / Claude 设置", translator("menu.codex"))
        self.assertEqual("日志（1 / 2）", translator("menu.logsSummary", {"recovering": 1, "cooldown": 2}))
        self.assertEqual("正在加载文档…", translator("common.secureEditorLoading"))
        self.assertEqual("provider-custom", translator("provider-custom"))

    def test_apply_refuses_an_external_disk_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "language.json"
            path.write_text('{"language":"system"}\n', encoding="utf-8")
            domain = LanguageSettingsDomain(path, system_locale="en-US")
            domain.dispatch("set", {"language": "en"})
            external = '{"language":"zh-Hans"}\n'
            path.write_text(external, encoding="utf-8")

            with self.assertRaisesRegex(LanguageSettingsError, "changed on disk"):
                domain.apply()

            self.assertEqual(external, path.read_text(encoding="utf-8"))

    def test_first_apply_refuses_a_file_created_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "language.json"
            domain = LanguageSettingsDomain(path, system_locale="en-US")
            domain.dispatch("set", {"language": "en"})
            external = '{"language":"zh-Hans"}\n'
            path.write_text(external, encoding="utf-8")

            with self.assertRaisesRegex(LanguageSettingsError, "changed on disk"):
                domain.apply()

            self.assertEqual(external, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
