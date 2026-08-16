from __future__ import annotations

import unittest

from litellm_menu.api_base import (
    api_base_for_surface,
    apply_surface_api_base,
    is_unversioned_anthropic_messages_endpoint,
    normalize_configured_api_base,
    service_root,
)


class APIBaseTests(unittest.TestCase):
    def test_normalize_configured_api_base_accepts_root_version_and_full_endpoint_forms(self) -> None:
        cases = {
            "https://api.example.test": "https://api.example.test/v1",
            "api.example.test": "https://api.example.test/v1",
            "https://api.example.test/": "https://api.example.test/v1",
            "https://api.example.test/v1": "https://api.example.test/v1",
            "https://api.example.test/v1/": "https://api.example.test/v1",
            "https://api.example.test/v1/chat/completions": "https://api.example.test/v1/chat/completions",
            "https://api.example.test/chat/completions/": "https://api.example.test/chat/completions",
            "https://api.example.test/v1/responses": "https://api.example.test/v1/responses",
            "https://api.example.test/v1/messages/": "https://api.example.test/v1/messages",
            "https://api.example.test/messages": "https://api.example.test/messages",
            "https://api.example.test/v1/completions": "https://api.example.test/v1/completions",
            "https://api.example.test/v1/images/generations": "https://api.example.test/v1/images/generations",
            "https://api.example.test/gateway/v1/messages": "https://api.example.test/gateway/v1/messages",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(normalize_configured_api_base(source), expected)

    def test_surface_base_removes_any_supplied_endpoint_without_duplicate_suffixes(self) -> None:
        cases = (
            ("https://api.example.test", "https://api.example.test/v1"),
            ("api.example.test", "https://api.example.test/v1"),
            ("https://api.example.test/v1", "https://api.example.test/v1"),
            ("https://api.example.test/v1/chat/completions", "https://api.example.test/v1"),
            ("https://api.example.test/chat/completions", "https://api.example.test"),
            ("https://api.example.test/v1/messages", "https://api.example.test/v1"),
            ("https://api.example.test/messages", "https://api.example.test"),
            ("https://api.example.test/v1/responses", "https://api.example.test/v1"),
            ("https://api.example.test/v1/completions", "https://api.example.test/v1"),
        )
        for surface in ("openai/responses", "openai/chat"):
            for source, expected in cases:
                with self.subTest(surface=surface, source=source):
                    self.assertEqual(api_base_for_surface(source, surface), expected)

    def test_anthropic_surface_uses_the_complete_messages_endpoint_once(self) -> None:
        cases = {
            "https://api.example.test": "https://api.example.test/v1/messages",
            "api.example.test": "https://api.example.test/v1/messages",
            "https://api.example.test/": "https://api.example.test/v1/messages",
            "https://api.example.test/v1": "https://api.example.test/v1/messages",
            "https://api.example.test/v1/": "https://api.example.test/v1/messages",
            "https://api.example.test/v1/messages": "https://api.example.test/v1/messages",
            "https://api.example.test/v1/chat/completions": "https://api.example.test/v1/messages",
            "https://api.example.test/messages": "https://api.example.test/messages",
            "https://api.example.test/gateway/v1/messages": "https://api.example.test/gateway/v1/messages",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(api_base_for_surface(source, "anthropic"), expected)

    def test_unversioned_anthropic_messages_endpoint_is_detected(self) -> None:
        self.assertTrue(
            is_unversioned_anthropic_messages_endpoint("https://api.example.test/messages")
        )
        self.assertTrue(
            is_unversioned_anthropic_messages_endpoint("https://api.example.test/gateway/messages")
        )
        self.assertFalse(
            is_unversioned_anthropic_messages_endpoint("https://api.example.test/v1/messages")
        )

    def test_surface_base_changes_selected_request_and_metadata_together(self) -> None:
        request = {
            "litellm_params": {"api_base": "https://api.example.test/v1/messages"},
            "litellm_metadata": {"api_base": "https://api.example.test/v1/messages"},
        }

        self.assertTrue(apply_surface_api_base(request, "openai/responses"))
        self.assertEqual(request["litellm_params"]["api_base"], "https://api.example.test/v1")
        self.assertEqual(request["api_base"], "https://api.example.test/v1")
        self.assertEqual(request["litellm_metadata"]["api_base"], "https://api.example.test/v1")

        self.assertTrue(apply_surface_api_base(request, "anthropic"))
        self.assertEqual(request["litellm_params"]["api_base"], "https://api.example.test/v1/messages")
        self.assertEqual(request["api_base"], "https://api.example.test/v1/messages")
        self.assertEqual(request["litellm_metadata"]["api_base"], "https://api.example.test/v1/messages")

    def test_gateway_path_names_are_not_mistaken_for_complete_endpoints(self) -> None:
        for path in ("model", "chat", "response", "tenant/model"):
            with self.subTest(path=path):
                source = f"https://api.example.test/{path}"
                self.assertEqual(normalize_configured_api_base(source), f"{source}/v1")

    def test_service_root_strips_versions_and_known_endpoints(self) -> None:
        cases = {
            "https://api.example.test": "https://api.example.test",
            "https://api.example.test/v1": "https://api.example.test",
            "https://api.example.test/gateway/v1/models": "https://api.example.test/gateway",
            "https://api.example.test/api/v1/responses": "https://api.example.test/api",
            "https://api.example.test/messages": "https://api.example.test",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(service_root(source), expected)

    def test_service_root_rejects_non_http_and_embedded_credentials(self) -> None:
        self.assertIsNone(service_root("api.example.test/v1"))
        self.assertIsNone(service_root("file:///tmp/api"))
        self.assertIsNone(service_root("https://user:password@api.example.test/v1"))



if __name__ == "__main__":
    unittest.main()
