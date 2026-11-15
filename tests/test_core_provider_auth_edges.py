from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from young_router.core import provider_auth
from young_router.core.provider_auth import ProviderAuthManager


class _OneShotAuthenticator:
    """The smallest Authenticator the device-code worker needs to finish."""

    def _request_device_code(self) -> dict[str, str]:
        return {"device_auth_id": "1", "user_code": "CODE-1", "interval": "1"}

    def _poll_for_authorization_code(self, device: dict[str, str]) -> dict[str, str]:
        return {"authorization_code": "authorization-1", "code_challenge": "challenge", "code_verifier": "verifier"}

    def _exchange_code_for_tokens(self, code: dict[str, str]) -> dict[str, str]:
        return {"access_token": "synthetic-access-token", "refresh_token": "synthetic-refresh-token", "id_token": ""}

    def _build_auth_record(self, tokens: dict[str, str]) -> dict[str, object]:
        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "id_token": "",
            "expires_at": int(time.time()) + 3_600,
        }


class ProviderAuthEdgesTests(unittest.TestCase):
    def test_a_superseded_attempt_does_not_claim_the_account_slot(self) -> None:
        # The credential and the active-account marker are two writes.  When a
        # cancel supersedes the attempt between them, the marker must not name
        # the account the person just cancelled: status() would then report it
        # signed in and active, and environment() would point the proxy at it.
        import litellm.llms.chatgpt.authenticator as chatgpt_authenticator

        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            ref = "openai-profile-superseded"
            manager._attempts[ref] = 1
            real_write = provider_auth.atomic_write_json

            def write_then_supersede(path, value, **kwargs) -> None:
                real_write(path, value, **kwargs)
                if isinstance(value, dict) and value.get("access_token") == "synthetic-access-token":
                    manager._attempts[ref] = 2

            with patch.object(chatgpt_authenticator, "Authenticator", _OneShotAuthenticator), patch.object(
                provider_auth, "atomic_write_json", write_then_supersede
            ):
                manager._run_chatgpt(ref, 1)

            self.assertFalse(manager._active_openai_file.exists())
            self.assertEqual("", manager.active_openai_ref())

    def test_status_reads_the_active_credentials_once(self) -> None:
        # The active account's credential is the same file status() just read
        # to answer for it; re-reading it to compare the active marker doubles
        # the file work for every projected provider row.
        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderAuthManager(Path(directory))
            ref = "openai-profile-active"
            auth_file = manager._secure_chatgpt_auth_file(ref, create=True)
            provider_auth.atomic_write_json(
                auth_file, {"access_token": "synthetic-access-token", "expires_at": 4102444800}
            )
            manager._write_active_openai_ref(ref)
            reads: list[Path] = []
            real_read = provider_auth.read_json

            def counting_read(path):
                reads.append(Path(path))
                return real_read(path)

            with patch.object(provider_auth, "read_json", counting_read):
                status = manager.status("openai_login", ref)

            self.assertTrue(status["active"])
            self.assertEqual(1, reads.count(auth_file))


if __name__ == "__main__":
    unittest.main()
