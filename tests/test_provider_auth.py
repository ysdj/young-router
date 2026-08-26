from __future__ import annotations

import stat
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from litellm_menu.core.persistence import atomic_write_json
from litellm_menu.core.provider_auth import ProviderAuthManager, credential_env_name


class ProviderAuthManagerTests(unittest.TestCase):
    def test_claude_browser_oauth_does_not_require_cli(self) -> None:
        token = "sk-ant-oat" + "browser-login-" + "a" * 20
        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            ref = "claude-browser-profile"
            exchange: dict[str, str] = {}

            def fake_exchange(*, code: str, code_verifier: str, state: str, redirect_uri: str):
                exchange.update(
                    code=code,
                    code_verifier=code_verifier,
                    state=state,
                    redirect_uri=redirect_uri,
                )
                return {
                    "access_token": token,
                    "refresh_token": "synthetic-refresh-token",
                    "expires_in": 3_600,
                    "refresh_token_expires_in": 7_200,
                    "scope": "user:profile user:inference",
                }

            with patch.object(manager, "_exchange_claude_code", side_effect=fake_exchange):
                manager.start("claude_login", ref)
                deadline = time.monotonic() + 3
                challenge: dict[str, object] = {}
                while time.monotonic() < deadline:
                    challenge = manager.status("claude_login", ref)
                    if challenge.get("verification_uri"):
                        break
                    time.sleep(0.01)
                self.assertEqual("authorizing", challenge.get("status"))
                verification_uri = str(challenge["verification_uri"])
                redirect_uri = str(challenge["redirect_uri"])
                authorization = urlparse(verification_uri)
                self.assertEqual("claude.com", authorization.hostname)
                self.assertEqual("/cai/oauth/authorize", authorization.path)
                params = parse_qs(authorization.query)
                self.assertEqual([redirect_uri], params["redirect_uri"])
                self.assertEqual(["S256"], params["code_challenge_method"])
                self.assertIn("user:inference", params["scope"][0])
                state = params["state"][0]

                with urlopen(f"{redirect_uri}?code=synthetic-code&state={state}", timeout=2) as response:
                    self.assertEqual(200, response.status)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and manager.status("claude_login", ref)["status"] == "authorizing":
                    time.sleep(0.01)

            self.assertEqual("signed_in", manager.status("claude_login", ref)["status"])
            self.assertEqual(token, manager.environment()[credential_env_name(ref)])
            self.assertEqual("synthetic-code", exchange["code"])
            self.assertEqual(redirect_uri, exchange["redirect_uri"])

    def test_claude_browser_oauth_rejects_wrong_callback_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            ref = "claude-state-profile"
            manager.start("claude_login", ref)
            deadline = time.monotonic() + 3
            challenge: dict[str, object] = {}
            while time.monotonic() < deadline:
                challenge = manager.status("claude_login", ref)
                if challenge.get("redirect_uri"):
                    break
                time.sleep(0.01)
            redirect_uri = str(challenge["redirect_uri"])
            with self.assertRaises(Exception):
                urlopen(f"{redirect_uri}?code=synthetic-code&state=wrong", timeout=2)
            self.assertEqual("authorizing", manager.status("claude_login", ref)["status"])
            manager.cancel(ref)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                worker = manager._threads.get(ref)
                if worker is None or not worker.is_alive():
                    break
                time.sleep(0.01)
            self.assertEqual("signed_out", manager.status("claude_login", ref)["status"])

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
