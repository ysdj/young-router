from __future__ import annotations

import stat
import threading
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from young_router.core.persistence import atomic_write_json
from young_router.core.provider_auth import ProviderAuthManager, credential_env_name


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

    def test_chatgpt_start_answers_authorizing_before_the_device_code_exists(self) -> None:
        # Device-code issuance is asynchronous: ``start`` answers out of the
        # same projection the host polls, and it answers while the worker has
        # no code yet. Indexing the two challenge keys there raised KeyError,
        # so the first read of every fresh sign-in failed and the login never
        # reached the WebView that would have shown the code.
        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            ref = "openai-profile-pending"
            release = threading.Event()
            started = threading.Event()

            def pending_worker(worker_ref: str, generation: int) -> None:
                started.set()
                release.wait(5)

            with patch.object(manager, "_run_chatgpt", side_effect=pending_worker):
                state = manager.start("openai_login", ref)
                self.assertTrue(started.wait(2))
                self.assertEqual("authorizing", state["status"])
                self.assertFalse(state["configured"])
                self.assertNotIn("user_code", state)
                self.assertNotIn("verification_uri", state)

                challenge = manager._set_state(
                    ref,
                    kind="openai_login",
                    status="authorizing",
                    configured=False,
                    verification_uri="https://auth.example.test/codex/device",
                    user_code="SYNTH-CODE",
                )
                release.set()

            self.assertEqual("SYNTH-CODE", challenge["user_code"])
            projected = manager.status("openai_login", ref)
            self.assertEqual("authorizing", projected["status"])
            self.assertEqual("https://auth.example.test/codex/device", projected["verification_uri"])
            self.assertEqual("SYNTH-CODE", projected["user_code"])

    def test_cancelled_chatgpt_attempt_never_blocks_or_overwrites_the_next_one(self) -> None:
        # LiteLLM's device-code poll cannot be interrupted, so a cancelled
        # attempt's worker outlives its cancel. While that worker was treated as
        # the live sign-in, every later press of 登录 answered with the cancelled
        # attempt's state and no challenge was ever requested; once it finally
        # completed, it wrote a credential for a flow the user had abandoned.
        import litellm.llms.chatgpt.authenticator as chatgpt_authenticator

        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            ref = "openai-profile-restart"
            polls: dict[str, threading.Event] = {}
            codes: dict[str, str] = {}
            issued: list[str] = []
            issued_lock = threading.Lock()

            class FakeAuthenticator:
                def _request_device_code(self) -> dict[str, str]:
                    with issued_lock:
                        index = str(len(issued) + 1)
                        issued.append(index)
                    polls[index] = threading.Event()
                    codes[index] = f"CODE-{index}"
                    return {"device_auth_id": index, "user_code": codes[index], "interval": "1"}

                def _poll_for_authorization_code(self, device: dict[str, str]) -> dict[str, str]:
                    polls[device["device_auth_id"]].wait(5)
                    return {
                        "authorization_code": f"authorization-{device['device_auth_id']}",
                        "code_challenge": "synthetic-challenge",
                        "code_verifier": "synthetic-verifier",
                    }

                def _exchange_code_for_tokens(self, code: dict[str, str]) -> dict[str, str]:
                    suffix = str(code["authorization_code"]).rsplit("-", 1)[-1]
                    return {
                        "access_token": f"synthetic-access-token-{suffix}",
                        "refresh_token": "synthetic-refresh-token",
                        "id_token": "",
                    }

                def _build_auth_record(self, tokens: dict[str, str]) -> dict[str, object]:
                    return {
                        "access_token": tokens["access_token"],
                        "refresh_token": tokens["refresh_token"],
                        "id_token": "",
                        "expires_at": int(time.time()) + 3_600,
                    }

            def wait_for_status(predicate, timeout: float = 3.0) -> dict[str, object]:
                deadline = time.monotonic() + timeout
                state: dict[str, object] = {}
                while time.monotonic() < deadline:
                    state = manager.status("openai_login", ref)
                    if predicate(state):
                        return state
                    time.sleep(0.01)
                return state

            with patch.object(chatgpt_authenticator, "Authenticator", FakeAuthenticator):
                manager.start("openai_login", ref)
                first = wait_for_status(lambda state: state.get("user_code") == "CODE-1")
                self.assertEqual("CODE-1", first.get("user_code"))

                manager.cancel(ref)
                self.assertEqual("signed_out", manager.status("openai_login", ref)["status"])

                # The first attempt's worker is still alive inside its poll; the
                # new press must still start a flow and publish its own code.
                manager.start("openai_login", ref)
                second = wait_for_status(lambda state: state.get("user_code") == "CODE-2")
                self.assertEqual("authorizing", second.get("status"))
                self.assertEqual("CODE-2", second.get("user_code"))

                # The abandoned attempt completing must write nothing: not the
                # credential, not the state of the sign-in on screen.
                polls["1"].set()
                time.sleep(0.3)
                self.assertEqual("authorizing", manager.status("openai_login", ref)["status"])
                auth_file = manager._chatgpt_auth_file(ref)
                self.assertNotIn("synthetic-access-token-1", auth_file.read_text(encoding="utf-8"))

                # The attempt the user is looking at is the one that imports.
                polls["2"].set()
                signed_in = wait_for_status(lambda state: state.get("status") != "authorizing")
                self.assertEqual("signed_in", signed_in["status"])
                self.assertIn("synthetic-access-token-2", auth_file.read_text(encoding="utf-8"))

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


    def test_status_names_the_account_its_credential_belongs_to(self) -> None:
        """A sign-out action needs the account's name, not the provider's.

        The identity is read from the credential's own id token and bounded;
        nothing else in that token is projected, and a token that states no
        address yields no account at all rather than an invented one.
        """

        import base64
        import json as json_module

        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            ref = "openai-profile-identity"
            payload = base64.urlsafe_b64encode(
                json_module.dumps({"email": "person@example.com", "sub": "secret-subject"}).encode("utf-8")
            ).decode("ascii").rstrip("=")
            id_token = f"header.{payload}.signature"
            auth_file = manager._secure_chatgpt_auth_file(ref, create=True)
            atomic_write_json(
                auth_file,
                {"access_token": "token", "id_token": id_token, "expires_at": 4102444800},
            )
            status = manager.status("openai_login", ref)
            self.assertEqual("person@example.com", status["account"])
            self.assertEqual("signed_in", status["status"])
            # The identity is the only thing projected from the token.
            self.assertNotIn("secret-subject", json_module.dumps(status))
            self.assertNotIn("id_token", status)

            atomic_write_json(auth_file, {"access_token": "token", "expires_at": 4102444800})
            self.assertNotIn("account", manager.status("openai_login", ref))

if __name__ == "__main__":
    unittest.main()
