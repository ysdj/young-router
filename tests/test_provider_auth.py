from __future__ import annotations

import threading
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from young_router.core.persistence import atomic_write_json
from young_router.core.provider_auth import ProviderAuthManager


class ProviderAuthManagerTests(unittest.TestCase):
    def test_a_claude_login_is_not_a_login_this_build_runs(self) -> None:
        """Claude's own OAuth is gone, and its absence is the whole contract.

        The manager used to own a second browser OAuth flow: a loopback
        callback server, a PKCE exchange, a token file, and an environment
        variable per profile.  A sign-in this build can no longer complete must
        not be startable at all — a `start` that half-ran would leave a
        provider waiting on a challenge nobody can answer.
        """

        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            with self.assertRaises(ValueError):
                manager.start("claude_login", "claude-profile")
            self.assertFalse(hasattr(manager, "import_claude_token"))
            self.assertFalse(hasattr(manager, "token"))
            # Nothing was staged for a flow that refused to start.
            self.assertFalse(
                (Path(directory) / ".litellm-runtime" / "provider-auth" / "claude-profile.json").exists()
            )

    def test_the_environment_carries_only_the_openai_session(self) -> None:
        """One login kind publishes an environment; nothing else may.

        The extra variables were one per Claude profile, read back out of the
        credential directory.  With that flow gone, anything left in the
        directory is not this app's credential to publish.
        """

        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            leftover = manager.root / "leftover-profile.json"
            leftover.write_text('{"kind": "claude_login", "token": "sk-ant-oat' + "a" * 24 + '"}', encoding="utf-8")
            environment = manager.environment()
            self.assertEqual(
                {"CHATGPT_TOKEN_DIR", "CHATGPT_AUTH_FILE"},
                set(environment),
            )
            self.assertNotIn("YOUNG_ROUTER_AUTH_", " ".join(environment))

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

    def test_an_aged_out_access_token_is_renewed_before_it_is_called_expired(self) -> None:
        """The refresh token outlives the access token, so expiry is a question.

        A ChatGPT sign-in mints an access token that lasts hours and a refresh
        token that lasts weeks.  Status used to compare ``expires_at`` and stop
        there, so every account read as 登录已过期 as soon as its first token
        aged out — while the grant behind it was still redeemable, and while
        the proxy adapter would have renewed it on the next request.  The
        projection now redeems the grant itself, and the pane only offers a
        re-login once the issuer actually refuses it.
        """

        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            ref = "openai-profile-renew"
            auth_file = manager._secure_chatgpt_auth_file(ref, create=True)
            atomic_write_json(
                auth_file,
                {
                    "access_token": "stale-access-token",
                    "refresh_token": "synthetic-refresh-token",
                    "expires_at": int(time.time()) - 60,
                },
            )
            exchanges: list[str] = []

            def redeem(self, refresh_token: str) -> dict[str, str]:
                exchanges.append(refresh_token)
                tokens = {
                    "access_token": "renewed-access-token",
                    "refresh_token": "rotated-refresh-token",
                    "id_token": "",
                }
                self._write_auth_file({**tokens, "expires_at": int(time.time()) + 3_600})
                return tokens

            import litellm.llms.chatgpt.authenticator as chatgpt_authenticator

            def settled(predicate, timeout: float = 3.0) -> dict[str, object]:
                deadline = time.monotonic() + timeout
                state: dict[str, object] = {}
                while time.monotonic() < deadline:
                    state = manager.status("openai_login", ref)
                    if predicate(state):
                        return state
                    time.sleep(0.01)
                return state

            with patch.object(chatgpt_authenticator.Authenticator, "_refresh_tokens", redeem):
                # A snapshot read must not wait on the issuer: it starts the
                # renewal and answers with what is on disk right now.
                first = manager.status("openai_login", ref)
                self.assertIn(first["status"], {"signed_in", "expired"})
                # The renewal lands in the background, and the next read states
                # it without spending a second exchange.
                renewed = settled(lambda state: state.get("status") == "signed_in")
                self.assertEqual("signed_in", renewed["status"])
                self.assertEqual(["synthetic-refresh-token"], exchanges)
                # A caller that needs the token gets it, and does not rotate the
                # grant again: the answer is already in hand.
                self.assertEqual("renewed-access-token", manager.chatgpt_request_credential(ref)["access_token"])
                self.assertEqual(1, len(exchanges))

    def test_a_refused_renewal_reports_expired_and_backs_off(self) -> None:
        """A grant the issuer refuses is the one case that asks for a re-login.

        That refusal must also stop the projection from re-asking the issuer on
        every render: status is read by the provider pane, the logs view and the
        scheduled check, and each read would otherwise spend a token exchange
        on an answer that is not going to change.
        """

        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            ref = "openai-profile-refused"
            auth_file = manager._secure_chatgpt_auth_file(ref, create=True)
            atomic_write_json(
                auth_file,
                {
                    "access_token": "stale-access-token",
                    "refresh_token": "refused-refresh-token",
                    "expires_at": int(time.time()) - 60,
                },
            )
            attempts: list[str] = []
            refused = True

            import litellm.llms.chatgpt.authenticator as chatgpt_authenticator

            def redeem(self, refresh_token: str) -> dict[str, str]:
                attempts.append(refresh_token)
                if refused:
                    raise chatgpt_authenticator.RefreshAccessTokenError(
                        message="synthetic refusal", status_code=400
                    )
                tokens = {
                    "access_token": "renewed-access-token",
                    "refresh_token": "rotated-refresh-token",
                    "id_token": "",
                }
                self._write_auth_file({**tokens, "expires_at": int(time.time()) + 3_600})
                return tokens

            with patch.object(chatgpt_authenticator.Authenticator, "_refresh_tokens", redeem):
                status = manager.status("openai_login", ref)
                self.assertIn(status["status"], {"signed_in", "expired"})
                # A signed-out projection is not what an expired grant is: the
                # credential is still there, it just cannot authenticate.
                self.assertTrue(status["configured"])
                # The refusal is remembered: the pane, the log view and the
                # scheduled check all project this row, and each read would
                # otherwise spend a token exchange on the same answer.
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and not attempts:
                    time.sleep(0.01)
                self.assertEqual("expired", manager.status("openai_login", ref)["status"])
                self.assertEqual(1, len(attempts))
                # The person's own 登录 is not held behind that wait: it clears
                # the backoff, and the token request that follows redeems the
                # grant because the person asked for it.
                refused = False
                with patch.object(manager, "_run_chatgpt"):
                    manager.start("openai_login", ref)
                credential = manager.chatgpt_request_credential(ref)
                self.assertEqual("renewed-access-token", credential["access_token"])
                self.assertEqual(2, len(attempts))
                self.assertEqual("signed_in", manager.status("openai_login", ref)["status"])
            self.assertNotIn(ref, manager._refresh_failed_at)

    def test_a_sign_in_in_progress_outranks_the_credential_it_replaces(self) -> None:
        """A re-login over an expired account must show its code, not the corpse.

        重新登录 starts a device code while the old, unusable credential is still
        on disk.  Reading the file first therefore kept answering 登录已过期, so
        the pane never entered 正在等待登录, never opened the sign-in window, and
        the code the issuer had already issued could not be entered.  Only a
        worker that is still alive counts: a record left behind by a restart
        describes no challenge.
        """

        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            ref = "openai-profile-relogin"
            auth_file = manager._secure_chatgpt_auth_file(ref, create=True)
            atomic_write_json(
                auth_file,
                {
                    "access_token": "stale-access-token",
                    "refresh_token": "refused-refresh-token",
                    "expires_at": int(time.time()) - 60,
                },
            )
            release = threading.Event()
            started = threading.Event()

            def pending_worker(worker_ref: str, generation: int) -> None:
                started.set()
                release.wait(5)

            try:
                with patch.object(manager, "_run_chatgpt", side_effect=pending_worker), patch.object(
                    manager, "_refresh_chatgpt_tokens", return_value=None
                ):
                    manager.start("openai_login", ref)
                    self.assertTrue(started.wait(2))
                    manager._set_state(
                        ref,
                        kind="openai_login",
                        status="authorizing",
                        configured=False,
                        verification_uri="https://auth.example.test/codex/device",
                        user_code="RELG-CODE",
                    )
                    state = manager.status("openai_login", ref)
                    self.assertEqual("authorizing", state["status"])
                    self.assertEqual("RELG-CODE", state["user_code"])
            finally:
                release.set()
            # Once no worker owns the flow, the credential's own state is what
            # the row reads again.  The worker that was patched out never
            # registered its thread, so there is nothing to wait for: the test
            # only asserts the projection no longer claims the challenge.
            state = manager.status("openai_login", ref)
            self.assertNotEqual("authorizing", state["status"])

if __name__ == "__main__":
    unittest.main()
