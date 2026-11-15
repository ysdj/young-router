"""Edge behaviour of the dsh-vision-router chain and the request it builds.

The bridge answers with rows this process did not build itself, so a row that
only dsh can reach, a row that names no token budget, and the compact profile
shapes earlier revisions exported all have to be answered here rather than
sent to a provider by accident.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from young_router.adapters import dsh_vision_router as router

PNG = "data:image/png;base64,cG5n"


class StagedChainTests(unittest.TestCase):
    def test_a_staged_row_without_a_url_is_not_routed(self) -> None:
        # A dsh adapter pair arrives as a row with an empty base URL because only
        # dsh can reach it.  Empty would silently mean the default OVH endpoint.
        private_row = {
            "name": "vision-http",
            "base_url": "",
            "model": "private-adapter",
            "max_tokens": None,
        }
        http_row = {"name": "ovh", "base_url": "https://example.test/v1", "model": "m"}
        with mock.patch.object(
            router, "_upstream_provider_chain", return_value=[private_row, http_row]
        ):
            with mock.patch.dict("os.environ", {"YOUNG_ROUTER_DSH_VISION_ROUTER_BACKEND": "auto"}):
                providers = router._configured_provider_chain()
        self.assertEqual([http_row], providers)

    def test_a_staged_null_budget_falls_back_to_the_configured_budget(self) -> None:
        with mock.patch.dict("os.environ", {"YOUNG_ROUTER_DSH_VISION_ROUTER_MAX_TOKENS": "999"}):
            payload = router._chat_completion_payload(
                PNG, {"model": "upstream-model", "max_tokens": None}
            )
        self.assertEqual(999, payload["max_tokens"])


class ConfigDocumentTests(unittest.TestCase):
    def test_a_compact_single_provider_reaches_the_worker_document(self) -> None:
        config = {"backend": "api", "provider": "ovh", "model": "ovh/Qwen3.5-397B-A17B"}
        with mock.patch.dict(
            "os.environ", {"YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON": json.dumps(config)}
        ):
            document = router._upstream_config_document()
        rows = [(entry["baseURL"], entry["model"]) for entry in document["httpProviders"]]
        self.assertEqual([(router._DEFAULT_HTTP_BASE_URL, "Qwen3.5-397B-A17B")], rows)

    def test_a_compact_provider_object_reaches_the_worker_document(self) -> None:
        provider = {"baseURL": "https://vision.example/v1", "model": "vision-model"}
        config = {"backend": "api", "provider": provider}
        with mock.patch.dict(
            "os.environ", {"YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON": json.dumps(config)}
        ):
            document = router._upstream_config_document()
        self.assertEqual([provider], document["httpProviders"])


class RequestPayloadTests(unittest.TestCase):
    def test_a_provider_system_instruction_precedes_the_image_turn(self) -> None:
        payload = router._chat_completion_payload(
            PNG, {"model": "vision-model", "max_tokens": 32, "system": "Answer in one line."}
        )
        self.assertEqual(
            {"role": "system", "content": "Answer in one line."}, payload["messages"][0]
        )
        self.assertEqual("user", payload["messages"][1]["role"])

    def test_a_configured_entry_keeps_its_system_instruction(self) -> None:
        item = router._provider_from_entry(
            {"baseURL": "https://vision.example/v1", "model": "vision-model", "system": "OCR only."}
        )
        self.assertIsNotNone(item)
        self.assertEqual("OCR only.", item["system"])


class RequestChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_provider_chain_is_resolved_once_for_every_image(self) -> None:
        calls: list[int] = []

        def chain() -> list[dict[str, object]]:
            calls.append(1)
            return [{"name": "ovh", "base_url": "https://example.test/v1", "model": "m"}]

        async def describe(reference: str, provider: dict) -> str:
            return f"description {reference[-1]}"

        images = [
            {"type": "image_url", "image_url": {"url": f"{PNG}a"}},
            {"type": "image_url", "image_url": {"url": f"{PNG}b"}},
        ]
        request = {"model": "text-only", "messages": [{"role": "user", "content": images}]}
        overrides = {
            "YOUNG_ROUTER_DSH_VISION_ROUTER_BACKEND": "auto",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_ENABLED": "1",
        }
        with mock.patch.object(router, "_configured_provider_chain", chain):
            with mock.patch.object(router, "_describe_with_provider", describe):
                with mock.patch.dict("os.environ", overrides):
                    rewritten = await router.dsh_vision_router_request_kwargs(request)
        self.assertEqual(1, len(calls))
        self.assertIsNotNone(rewritten)


if __name__ == "__main__":
    unittest.main()
