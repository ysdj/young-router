from __future__ import annotations

from hook_test_utils import *


class HookVisionBridgeTests(HookTestCase):
    async def test_backend_compat_reads_legacy_mode(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env("LITELLM_MENU_VISION_BRIDGE_BACKEND", None)
        self.set_env("LITELLM_MENU_VISION_BRIDGE_MODE", "off")

        self.assertEqual("off", hooks._bridge_backend())

    async def test_local_backend_reads_data_url_without_api(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env("LITELLM_MENU_VISION_BRIDGE_BACKEND", "local")
        self.set_env("LITELLM_MENU_VISION_BRIDGE_LOCAL_FORMAT", "compact")

        def local_description(reference: str) -> str:
            self.assertTrue(reference.startswith("data:image/png;base64,"))
            return "Layout summary:\ntype=form/settings page\nregions=top header band, left navigation rail\nelements=sidebar, header, form, inputs≈2, buttons≈1\ninputs=Email | Password\nbuttons=Sign in\npreview=Menu | Sign in | Home | Settings | Email | Password\n\nVisible text:\nSign in\nEmail\nPassword"

        hooks._local_vision_description = local_description
        request = {
            "model": "text-only",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is shown?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ],
        }

        rewritten = await hooks.bridged_request_kwargs(request)

        self.assertIsNotNone(rewritten)
        dumped = json.dumps(rewritten)
        self.assertIn("type=form/settings page", dumped)
        self.assertIn("elements=sidebar, header, form", dumped)
        self.assertIn("Visible text", dumped)
        self.assertIn("Password", dumped)
        self.assertNotIn("image_url", dumped)

    async def test_local_backend_detailed_format_is_preserved(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env("LITELLM_MENU_VISION_BRIDGE_BACKEND", "local")
        self.set_env("LITELLM_MENU_VISION_BRIDGE_LOCAL_FORMAT", "detailed")

        def local_description(reference: str) -> str:
            self.assertTrue(reference.startswith("data:image/png;base64,"))
            return "Layout summary:\nImage size: 900x600.\nProbable page type: dialog or modal surface.\nDetected elements: 3 OCR text line(s), modal-style central surface, about 1 button-like region(s).\nLikely button labels: Confirm"

        hooks._local_vision_description = local_description
        request = {
            "model": "text-only",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is shown?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ],
        }

        rewritten = await hooks.bridged_request_kwargs(request)

        self.assertIsNotNone(rewritten)
        dumped = json.dumps(rewritten)
        self.assertIn("Probable page type", dumped)
        self.assertIn("Likely button labels", dumped)

    async def test_auto_backend_falls_back_to_local_when_api_fails(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env("LITELLM_MENU_VISION_BRIDGE_BACKEND", "auto")

        def fail_api(payload: dict) -> str:
            raise RuntimeError("connection refused")

        def local_description(reference: str) -> str:
            return "spreadsheet with totals"

        hooks._post_chat_completion = fail_api
        hooks._local_vision_description = local_description
        request = {
            "model": "text-only",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "read this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ],
        }

        rewritten = await hooks.bridged_request_kwargs(request)

        self.assertIsNotNone(rewritten)
        self.assertIn("spreadsheet with totals", json.dumps(rewritten))

    async def test_should_attempt_vision_bridge_for_image_unsupported_error(self) -> None:
        hooks, _ = load_hook_module()
        error = RuntimeError("model does not support image input")
        error.status_code = 400
        request = {
            "model": "text-only",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "read this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ],
        }

        self.assertTrue(hooks.should_attempt_vision_bridge(error, request))

    async def test_should_attempt_vision_bridge_for_openrouter_no_image_endpoint_error(self) -> None:
        hooks, _ = load_hook_module()
        error = RuntimeError('{"error":{"message":"no endpoints found that support image input","code":404}}')
        error.status_code = 404
        request = {
            "model": "text-only",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "read this"},
                        {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                    ],
                }
            ],
        }

        self.assertTrue(hooks.should_attempt_vision_bridge(error, request))

    async def test_responses_request_is_rewritten_to_text_only_visual_context(self) -> None:
        hooks, _ = load_hook_module()

        async def describe(reference: str) -> str:
            return f"description for {reference[-3:]}"

        hooks._describe_image = describe
        request = {
            "model": "text-only",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "read this"},
                        {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                    ],
                }
            ],
        }

        rewritten = await hooks.bridged_request_kwargs(request)

        self.assertIsNotNone(rewritten)
        dumped = json.dumps(rewritten)
        self.assertNotIn("input_image", dumped)
        self.assertIn("local vision bridge produced this visual context", dumped)
        self.assertIn("description for abc", dumped)
        self.assertTrue(rewritten["litellm_metadata"]["vision_bridge_attempted"])

    async def test_bridge_request_copy_tolerates_runtime_ssl_context(self) -> None:
        hooks, _ = load_hook_module()
        import ssl

        async def describe(reference: str) -> str:
            return "tiny screenshot"

        ssl_context = ssl.create_default_context()
        hooks._describe_image = describe
        request = {
            "model": "text-only",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "read this"},
                        {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                    ],
                }
            ],
            "ssl_context": ssl_context,
            "litellm_params": {
                "proxy_server_request": {
                    "headers": {"session-id": "thread-with-image"},
                    "ssl_context": ssl_context,
                }
            },
        }

        rewritten = await hooks.bridged_request_kwargs(request)

        self.assertIs(rewritten["ssl_context"], ssl_context)
        self.assertIs(rewritten["litellm_params"]["proxy_server_request"]["ssl_context"], ssl_context)
        dumped = json.dumps(rewritten, default=str)
        self.assertNotIn("input_image", dumped)
        self.assertIn("tiny screenshot", dumped)

    async def test_bridge_request_copy_tolerates_runtime_sets_after_computer_use_image(self) -> None:
        hooks, _ = load_hook_module()

        class RuntimeMarker:
            def __hash__(self) -> int:
                return 1

            def __deepcopy__(self, memo):
                return {"runtime": "marker"}

        async def describe(reference: str) -> str:
            self.assertTrue(reference.startswith("data:image/jpeg;base64,"))
            return "Finder window with a project folder"

        hooks._describe_image = describe
        request = {
            "model": "text-only",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "运行一次computer use试试"}]},
                {"type": "function_call", "name": "get_app_state", "call_id": "call_state"},
                {
                    "type": "function_call_output",
                    "call_id": "call_state",
                    "output": [
                        {"type": "input_text", "text": "Computer Use state (CUA App Version: 857)"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/jpeg;base64,/9j/abc",
                            "detail": "high",
                        },
                    ],
                },
            ],
            "_runtime_markers": {RuntimeMarker()},
        }

        rewritten = await hooks.bridged_request_kwargs(request)

        self.assertIsNotNone(rewritten)
        self.assertEqual(1, len(rewritten["_runtime_markers"]))
        dumped = json.dumps(rewritten, default=str)
        self.assertNotIn("input_image", dumped)
        self.assertIn("Finder window with a project folder", dumped)

    async def test_chat_request_is_rewritten_to_text_only_visual_context(self) -> None:
        hooks, _ = load_hook_module()

        async def describe(reference: str) -> str:
            return "screen with a login form"

        hooks._describe_image = describe
        request = {
            "model": "text-only",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is shown?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ],
        }

        rewritten = await hooks.bridged_request_kwargs(request)

        self.assertIsNotNone(rewritten)
        dumped = json.dumps(rewritten)
        self.assertNotIn("image_url", dumped)
        self.assertIn("screen with a login form", dumped)

    async def test_retry_with_vision_bridge_calls_original_function_with_rewritten_request(self) -> None:
        hooks, _ = load_hook_module()
        seen = {}

        async def describe(reference: str) -> str:
            return "a chart with revenue labels"

        async def original_function(**kwargs):
            seen["kwargs"] = kwargs
            return {"ok": True}

        hooks._describe_image = describe
        request = {
            "model": "text-only",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "summarize"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ],
        }

        response = await hooks.retry_with_vision_bridge(original_function, request)

        self.assertEqual({"ok": True}, response)
        dumped = json.dumps(seen["kwargs"])
        self.assertNotIn("image_url", dumped)
        self.assertIn("a chart with revenue labels", dumped)
