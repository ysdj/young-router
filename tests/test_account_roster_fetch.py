import json, tempfile, unittest
from pathlib import Path
from unittest import mock
from young_router.core.domains import providers_models as pm
from young_router.core.provider_auth import ProviderAuthManager


class AccountRosterTests(unittest.TestCase):
    def make(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".litellm-runtime" / "provider-auth" / "chatgpt" / "provider-auth-x").mkdir(parents=True)
        (tmp / ".litellm-runtime" / "provider-auth" / "chatgpt" / "provider-auth-x" / "auth.json").write_text(json.dumps({
            "access_token": "tok", "account_id": "acc", "expires_at": 9999999999,
            "refresh_token": "r", "id_token": "i"}))
        cfg = tmp / "config.yaml"
        cfg.write_text(
            "providers:\n"
            "  OpenAI:\n"
            "    enabled: true\n"
            "    x-young-router-provider-auth:\n"
            "      kind: openai_login\n"
            "      credential_ref: provider-auth-x\n"
            "model_list: []\n"
        )
        return pm.ProvidersModelsDomain(cfg, auth_manager=ProviderAuthManager(tmp))

    def test_the_account_answers_its_own_roster(self):
        domain = self.make()
        answer = mock.MagicMock()
        answer.__enter__.return_value.status = 200
        answer.__enter__.return_value.read.return_value = json.dumps({"models": [{"slug": "gpt-5.4"}, {"slug": "gpt-5.1"}]}).encode()
        opener = mock.MagicMock()
        opener.open.return_value = answer
        with mock.patch.object(pm, "isolated_http_opener", return_value=opener):
            out = domain.dispatch("providers.fetch_models", {"provider_id": "OpenAI", "account_ref": "provider-auth-x"})
        summary = out["operation_summary"]
        self.assertEqual(["gpt-5.4", "gpt-5.1"], summary["models"])
        self.assertEqual("provider-auth-x", summary["account_ref"])
        sent = opener.open.call_args.args[0]
        # The roster endpoint refuses a caller that does not name its client.
        self.assertTrue(sent.full_url.startswith("https://chatgpt.com/backend-api/codex/models?client_version="))
        self.assertEqual("Bearer tok", sent.headers["Authorization"])
        self.assertEqual("acc", sent.headers["Chatgpt-account-id"])

    def test_an_empty_roster_is_an_answer_not_a_failure(self):
        domain = self.make()
        answer = mock.MagicMock()
        answer.__enter__.return_value.status = 200
        answer.__enter__.return_value.read.return_value = b'{"models": []}'
        opener = mock.MagicMock()
        opener.open.return_value = answer
        with mock.patch.object(pm, "isolated_http_opener", return_value=opener):
            out = domain.dispatch("providers.fetch_models", {"provider_id": "OpenAI", "account_ref": "provider-auth-x"})
        summary = out["operation_summary"]
        self.assertTrue(summary["available"])
        self.assertEqual(0, summary["model_count"])
        self.assertIn("lists no models", summary["detail"])


if __name__ == "__main__":
    unittest.main()
