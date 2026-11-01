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
    def test_a_model_the_account_does_not_serve_is_named(self):
        """The account answered; it just does not serve that model.

        A signed-in ChatGPT account serves its own roster, and the route on
        screen named `gpt-5.4`, which that roster does not carry.  The endpoint
        says so with 400 `model_not_found`, and reporting that as "上游报错"
        left the reader with no idea that the route was the thing to change.
        """

        import urllib.error

        body = json.dumps({
            "detail": "The 'gpt-5.4' model is not supported when using Codex with a ChatGPT account.",
            "error": {
                "type": "invalid_request_error",
                "code": "model_not_found",
                "message": "The 'gpt-5.4' model is not supported when using Codex with a ChatGPT account.",
            },
        }).encode()
        with mock.patch.object(
            pm,
            "isolated_http_opener",
            return_value=mock.MagicMock(
                open=mock.MagicMock(
                    side_effect=urllib.error.HTTPError(
                        "https://chatgpt.com/backend-api/codex/responses", 400, "Bad Request", {},
                        mock.MagicMock(read=lambda *_args: body),
                    )
                )
            ),
        ):
            result = pm.ProvidersModelsDomain._surface_probe(
                surface="openai/responses",
                api_base=pm.ProvidersModelsDomain._CHATGPT_API_BASE,
                credential="tok",
                model_name="gpt-5.4",
                account_id="acc",
            )
        self.assertEqual("model_unavailable", result["status"])
        self.assertFalse(result["available"])

    def test_an_account_route_is_probed_on_the_account_s_own_door(self):
        """An account row stores no base: its adapter owns the address.

        The probe read the row's *key* and the row's (empty) base, so every
        account-backed route asked an empty address with a placeholder key and
        reported "配置不完整" for a route that served fine.
        """

        domain = self.make()
        provider = domain._draft["providers"][0]
        provider["models"] = [{
            "model_name": "gpt-5.6-luna",
            "litellm_model": "chatgpt/gpt-5.6-luna",
            "account_ref": "provider-auth-x",
            "upstream_url_surface": "openai/responses",
            "upstream_protocol_mode": "fixed",
            "enabled": True,
        }]
        seen: list[dict[str, object]] = []

        def surface_probe(**kwargs: object) -> dict[str, object]:
            seen.append(kwargs)
            return {
                "surface": kwargs["surface"],
                "available": True,
                "status": "ok",
                "original_request": {},
            }

        with mock.patch.object(pm.ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe):
            result = domain.probe({"provider_id": "OpenAI", "model_id": "gpt-5.6-luna"})
        self.assertEqual(1, len(seen))
        self.assertEqual("openai/responses", seen[0]["surface"])
        self.assertEqual("tok", seen[0]["credential"])
        self.assertEqual("https://chatgpt.com/backend-api/codex", seen[0]["api_base"])
        self.assertEqual("acc", seen[0]["account_id"])
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
