from __future__ import annotations

import stat
from pathlib import Path
import tempfile
import unittest

from litellm_menu.core.persistence import atomic_write_json
from litellm_menu.core.provider_auth import ProviderAuthManager, credential_env_name


class ProviderAuthManagerTests(unittest.TestCase):
    def test_claude_token_is_private_and_not_projected(self) -> None:
        token = "sk-ant-oat" + "synthetic-token-" + "a" * 16
        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            ref = "claude-profile"
            result = manager.import_claude_token(ref, token)
            self.assertEqual("signed_in", result["status"])
            self.assertEqual("signed_in", manager.status("claude_login", ref)["status"])
            self.assertEqual(token, manager.environment()[credential_env_name(ref)])

            credential_file = Path(directory) / ".litellm-runtime" / "provider-auth" / f"{ref}.json"
            self.assertEqual(0o600, stat.S_IMODE(credential_file.stat().st_mode))
            self.assertNotIn(token, str(manager.status("claude_login", ref)))

            with self.assertRaises(ValueError):
                manager.import_claude_token(ref, "sk-ant-oat")
            with self.assertRaises(ValueError):
                manager.import_claude_token(ref, "sk-ant-oat" + "short")
            with self.assertRaises(ValueError):
                manager.import_claude_token(ref, token + "!")

    def test_chatgpt_accounts_are_independent_and_activation_selects_runtime_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            first = "openai-profile-one"
            second = "openai-profile-two"
            first_file = manager._secure_chatgpt_auth_file(first, create=True)
            second_file = manager._secure_chatgpt_auth_file(second, create=True)
            atomic_write_json(first_file, {"access_token": "synthetic-access-token-one", "expires_at": 4102444800})
            atomic_write_json(second_file, {"access_token": "synthetic-access-token-two", "expires_at": 4102444800})
            self.assertEqual("signed_in", manager.status("openai_login", first)["status"])
            self.assertEqual("signed_in", manager.status("openai_login", second)["status"])
            self.assertNotEqual(first_file, second_file)
            self.assertEqual("", manager.active_openai_ref())

            manager.activate("openai_login", first)
            self.assertEqual(first, manager.active_openai_ref())
            self.assertEqual(str(first_file.parent), manager.environment()["CHATGPT_TOKEN_DIR"])
            self.assertTrue(manager.status("openai_login", first)["active"])
            self.assertFalse(manager.status("openai_login", second)["active"])

            manager.activate("openai_login", second)
            self.assertEqual(second, manager.active_openai_ref())
            self.assertEqual(str(second_file.parent), manager.environment()["CHATGPT_TOKEN_DIR"])
            self.assertFalse(manager.status("openai_login", first)["active"])
            self.assertTrue(manager.status("openai_login", second)["active"])

            manager.logout("openai_login", first)
            self.assertFalse(first_file.exists())
            self.assertEqual(second, manager.active_openai_ref())
            self.assertEqual("signed_in", manager.status("openai_login", second)["status"])

            manager.logout("openai_login", second)
            self.assertEqual("", manager.active_openai_ref())

    def test_legacy_chatgpt_auth_file_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            auth_file = manager._secure_chatgpt_auth_file(create=True)
            atomic_write_json(auth_file, {"access_token": "synthetic-access-token", "expires_at": 4102444800})
            self.assertEqual("signed_in", manager.status("openai_login", "chatgpt-account")["status"])
            manager.logout("openai_login", "chatgpt-account")
            self.assertFalse(auth_file.exists())

    def test_chatgpt_auth_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            auth_file = manager._chatgpt_auth_file()
            outside = Path(directory) / "outside.json"
            outside.write_text("{}\n", encoding="utf-8")
            auth_file.symlink_to(outside)
            with self.assertRaises(ValueError):
                manager.status("openai_login", "chatgpt-account")

    def test_chatgpt_account_directory_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            outside = Path(directory) / "outside"
            outside.mkdir()
            account = manager.chatgpt_root / "profile"
            account.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                manager.status("openai_login", "profile")


if __name__ == "__main__":
    unittest.main()
